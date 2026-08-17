"""安全工具：路径净化、zip-slip 防护、URL 白名单、哈希、可执行文件校验。"""
import hashlib
import ipaddress
import os
import re
import socket
import zipfile
from pathlib import Path

from .audit import get_logger

_log = get_logger()

# 下载域名白名单（默认仅官网；子域名同样允许）
ALLOWED_HOSTS = {"flingtrainer.com"}
# Steam 封面图 CDN 白名单（storesearch/appdetails 返回的 header_image / tiny_image）
STEAM_IMAGE_HOSTS = {"steamstatic.com", "steamcdn-a.akamaihd.net"}
# Epic 封面图 CDN 白名单（store.epicgames.com/graphql 返回的 keyImages.url，
# 图片位于 cdn1.epicgames.com 等子域）
EPIC_IMAGE_HOSTS = {"epicgames.com"}
# 封面图显示层白名单（Steam + Epic 合并）
COVER_IMAGE_HOSTS = frozenset(STEAM_IMAGE_HOSTS) | frozenset(EPIC_IMAGE_HOSTS)
# Steam 商店 API 域名白名单（storesearch / appdetails 接口）
STEAM_API_HOSTS = {"steampowered.com"}

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
             "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
_MAX_NAME_LEN = 80
_MAX_EXTRACT_BYTES = 64 * 1024 * 1024  # 单个压缩条目解压上限 64MB（zip 炸弹防护）


def sanitize_component(name) -> str:
    """净化用于目录/文件名的单级名称：去非法字符、防保留名、限长。"""
    name = _INVALID_CHARS.sub("_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    name = name[:_MAX_NAME_LEN]
    if not name:
        name = "未命名"
    if name.upper() in _RESERVED:
        name = "_" + name
    return name


def unique_component(name, existing) -> str:
    """在 existing（目录名集合）中返回不冲突的名字。
    同名冲突时追加" (2)"" (3)"等后缀，避免不同游戏修改器混进同一目录。"""
    name = sanitize_component(name)
    existing = {str(e).casefold() for e in existing} if existing else set()
    if name.casefold() not in existing:
        return name
    i = 2
    while True:
        candidate = f"{name} ({i})"
        if candidate.casefold() not in existing:
            return candidate
        i += 1


def sha256_file(path, chunk=1024 * 1024) -> str:
    """流式计算文件 SHA-256，避免大文件整体驻留内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _host_matches(host, domains) -> bool:
    """host 命中 domains 中任意域名（含子域名）。"""
    return host in domains or any(host.endswith("." + d) for d in domains)


def _host_has_private_ip(host: str) -> bool:
    """解析 host 全部 IP；任一为私网/环回/链路本地/保留地址返回 True。
    解析失败按不安全处理（宁可拒绝下载封面）。"""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return True
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_unspecified or addr.is_reserved):
            return True
    return False


def url_allowed(url) -> bool:
    """校验下载 URL：仅 HTTPS，host 命中白名单（含子域名）。"""
    if not url or not url.startswith("https://"):
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "flingtrainer.com" or host.endswith(".flingtrainer.com")


def cover_url_allowed(url) -> bool:
    """校验封面图 URL：仅 HTTPS，host 命中 Steam 图片 CDN（含子域名），
    且解析后 IP 不得命中私网/环回/链路本地（防 SSRF / DNS rebinding）。
    调用前应先做本校验，再进行实际请求。"""
    if not url or not url.startswith("https://"):
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not _host_matches(host, STEAM_IMAGE_HOSTS):
        return False
    return not _host_has_private_ip(host)


def safe_get(url, allowed_domains, *, timeout=12, max_hops=4,
             max_bytes=None, headers=None, method="GET", json_body=None,
             label="") -> bytes:
    """安全请求：仅 HTTPS + host 命中 allowed_domains + 解析后 IP 非私网/环回，
    手动跟随重定向且每一跳重新校验（防 DNS rebinding / 跳往内网）。
    支持 method/json_body（POST GraphQL 等），body 仅在第一跳发送，跳转后不重发。
    仅返回 200 响应体（可选限长，分块读取限制峰值内存）；
    任何校验失败或网络异常均返回 b""。

    label：供排查用的业务标签（如 "Epic封面搜索"），失败时写进 audit.log，
    方便根据状态码/域名/错误类型定位"为什么拉不到封面"。

    失败路径都返回 b""，但都会写一行 warning 日志：
    - 非 https / 域名不在白名单 / 解析到私网 IP（SSRF 拒绝）；
    - HTTP 非 200（含 403/429 等，附状态码）；
    - 网络/请求异常（附异常类型与原因）。
    """
    import requests
    from urllib.parse import urljoin, urlparse
    current = url
    session = requests.Session()
    try:
        for _hop in range(max_hops):
            if not current.startswith("https://"):
                _log.warning("safe_get 拒绝非https %s %s", label, current[:80])
                return b""
            try:
                host = (urlparse(current).hostname or "").lower()
            except Exception:
                _log.warning("safe_get URL解析失败 %s %s", label, current[:80])
                return b""
            if not _host_matches(host, allowed_domains):
                _log.warning("safe_get 域名不在白名单 %s host=%s",
                             label, host)
                return b""
            if _host_has_private_ip(host):
                _log.warning("safe_get DNS解析到私网/环回 %s host=%s",
                             label, host)
                return b""
            body = json_body if _hop == 0 else None
            try:
                with session.request(method, current, timeout=timeout, verify=True,
                                     allow_redirects=False, stream=True, json=body,
                                     headers=headers or {"User-Agent": "TrainerHub/1.0"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            _log.warning("safe_get 重定向无Location %s host=%s",
                                         label, host)
                            return b""
                        current = urljoin(current, loc)
                        continue
                    if r.status_code != 200:
                        _log.warning("safe_get 非200 %s host=%s status=%s",
                                     label, host, r.status_code)
                        return b""
                    if max_bytes is None:
                        return r.content
                    # 分块读取并按 max_bytes 截断，避免先读完整响应导致峰值内存超限
                    chunks = []
                    remaining = max_bytes
                    for block in r.iter_content(64 * 1024):
                        if not block:
                            break
                        if len(block) > remaining:
                            chunks.append(block[:remaining])
                            break
                        chunks.append(block)
                        remaining -= len(block)
                        if remaining <= 0:
                            break
                    return b"".join(chunks)
            except requests.RequestException as e:
                _log.warning("safe_get 请求异常 %s host=%s err=%s:%s",
                             label, host, type(e).__name__, e)
                return b""
    except Exception as e:
        _log.warning("safe_get 未预期异常 %s err=%s:%s",
                     label, type(e).__name__, e)
        return b""
    finally:
        try:
            session.close()
        except Exception:
            pass
    return b""


def safe_extract_zip(zip_path, dest_dir) -> list:
    """安全解压：阻止 zip-slip（../、绝对路径、盘符、越界）。
    返回解压出的文件绝对路径列表。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
                raise ValueError(f"拒绝非法压缩条目: {info.filename!r}")
            if ".." in name.split("/"):
                raise ValueError(f"拒绝越界条目: {info.filename!r}")
            # 拒绝 NTFS 备用数据流（"a.exe:stream" 可污染已存在文件）
            if ":" in name:
                raise ValueError(f"拒绝非法压缩条目: {info.filename!r}")
            base = name.split("/")[-1]
            # 拒绝 Windows 设备名（CON/NUL/COM1…，含 "CON.exe" 形式）
            if base.upper().split(".")[0] in _RESERVED:
                raise ValueError(f"拒绝设备名条目: {info.filename!r}")
            if info.file_size > _MAX_EXTRACT_BYTES:
                raise ValueError(f"拒绝超大条目: {info.filename!r}")
            # CWE-22 防护：仅取条目文件名并经 sanitize_component 净化
            # （去分隔符/../非法字符/保留名，限长），拼接到解压根目录，
            # 目标路径必然位于其内，不存在路径穿越面
            fname = sanitize_component(base)
            target = dest_dir / fname
            target.write_bytes(zf.read(info))
            extracted.append(target)
    return extracted


def find_exe_in_dir(directory) -> list:
    """在目录内递归查找 .exe，按大小降序（主程序通常较大）。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    cands = []
    for p in directory.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".exe":
            try:
                cands.append((p, p.stat().st_size))
            except OSError:
                pass
    cands.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in cands]

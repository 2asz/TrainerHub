"""安全工具：路径净化、zip-slip 防护、URL 白名单、哈希、可执行文件校验。"""
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .audit import get_logger

_log = get_logger()

# 下载域名白名单（默认仅官网；子域名同样允许）
ALLOWED_HOSTS = {"flingtrainer.com"}

# 浏览器 User-Agent（网络模块唯一来源，各处引用同一份）：
# 完整 Chrome 头才能过 Cloudflare 等防护（实测 UA 不完整会被 403）
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Steam 封面图 CDN 白名单（storesearch/appdetails 返回的 header_image / tiny_image）
STEAM_IMAGE_HOSTS = {"steamstatic.com", "steamcdn-a.akamaihd.net"}
# Epic 封面图 CDN 白名单（store.epicgames.com/graphql 返回的 keyImages.url，
# 图片位于 cdn1.epicgames.com 等子域；国内解析常返回腾讯云镜像
# cdn1-epicgames-<bucket>.file.myqcloud.com，精确加白该 host）
EPIC_IMAGE_HOSTS = {"epicgames.com",
                    "cdn1-epicgames-1251447533.file.myqcloud.com"}
# 封面图显示层白名单（Steam + Epic 合并）
COVER_IMAGE_HOSTS = frozenset(STEAM_IMAGE_HOSTS) | frozenset(EPIC_IMAGE_HOSTS)
# Steam 商店 API 域名白名单（storesearch / appdetails 接口）
STEAM_API_HOSTS = {"steampowered.com"}

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
             "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
_MAX_NAME_LEN = 80
_MAX_EXTRACT_BYTES = 64 * 1024 * 1024   # 单个压缩条目解压上限 64MB（zip 炸弹防护）
_MAX_EXTRACT_TOTAL = 512 * 1024 * 1024  # 全包总解压上限 512MB（防海量小文件膨胀）
_MAX_EXTRACT_FILES = 2000               # 条目数上限


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


def url_allowed(url, hosts=None) -> bool:
    """校验下载 URL：仅 HTTPS，host 命中 hosts（含子域名，缺省
    ALLOWED_HOSTS），且解析后 IP 不得为私网/环回/链路本地（与 safe_get
    的 SSRF 防护一致——下载器手动跟随重定向，每一跳都会重新调用本校验）。"""
    if not url or not url.startswith("https://"):
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not _host_matches(host, hosts or ALLOWED_HOSTS):
        return False
    return not _host_has_private_ip(host)


# 模块级复用的 Session：批量拉封面/更新检查时避免每次请求重建 TLS 连接
#（2026-09-04 审查优化项）。requests 连接池线程安全，本项目无 cookie 逻辑，
# 多线程共享无副作用；懒创建以保持 requests 的延迟导入。
_SAFE_SESSION = None


def _safe_session():
    """返回共享 Session。trust_env=False：忽略系统代理环境变量（HTTP_PROXY
    等）——走代理时实际连接由代理发起，私网 IP 校验会被整个绕过
    （2026-09-02 审查 P2-2 附带项）。"""
    global _SAFE_SESSION
    if _SAFE_SESSION is None:
        import requests
        s = requests.Session()
        s.trust_env = False
        _SAFE_SESSION = s
    return _SAFE_SESSION


def safe_get(url, allowed_domains, *, timeout=12, max_hops=4,
             max_bytes=None, headers=None, method="GET", json_body=None,
             label="") -> bytes:
    """安全请求：仅 HTTPS + host 命中 allowed_domains + 解析后 IP 非私网/环回，
    手动跟随重定向且每一跳重新校验（防 DNS rebinding / 跳往内网）。
    支持 method/json_body（POST GraphQL 等）。重定向转发策略与浏览器一致：
    301/302/303 降级 GET 且不再携带 body；307/308 原样保留 method 与 body。
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
    current_body = json_body         # 307/308 重定向按 RFC 原样重发 body
    session = _safe_session()
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
            body = current_body
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
                        if r.status_code in (301, 302, 303):
                            method = "GET"   # 与浏览器/requests 语义一致：原 POST 不带 body 重发没有意义
                            current_body = None
                        # 307/308：method 与 body 原样保留（RFC 7231 语义）
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
    return b""


def _zip_entry_count(path) -> int | None:
    """从 EOCD 尾部解析 zip 条目总数（只读 64KB 尾巴，不落盘任何东西）。

    必须在 `zipfile.ZipFile()` 构造**之前**调用：构造会把整个 central
    directory 物化成 Python 对象（每条约 1KB 内存）——构造包可在
    _MAX_EXTRACT_FILES 检查生效前先把内存打爆（2026-09-13 审查 P2）。
    ZIP64/损坏包返回 None（跳过预检，事后复检兜底）。"""
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 66000))
            tail = fh.read()
        i = tail.rfind(b"PK\x05\x06")          # EOCD 签名
        if i < 0 or i + 22 > len(tail):
            return None
        return int.from_bytes(tail[i + 10:i + 12], "little")
    except OSError:
        return None


def safe_extract_zip(zip_path, dest_dir) -> list:
    """安全解压：阻止 zip-slip（../、绝对路径、盘符、越界、设备名、ADS）。
    条目扁平化到 dest_dir；与已有文件/彼此同名的条目用唯一化改名，绝不静默覆盖。
    先解到临时目录再整体迁入，中途失败不留半包。
    返回解压出的文件绝对路径列表。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 构造 ZipFile 前先从 EOCD 拿条目总数：构造函数会把整个 central
    # directory 物化，条目数防线必须在它之前（2026-09-13 审查 P2）
    count = _zip_entry_count(zip_path)
    if count is not None and count > _MAX_EXTRACT_FILES:
        raise ValueError(f"压缩包条目数超过上限（{_MAX_EXTRACT_FILES}）")
    with tempfile.TemporaryDirectory(prefix="th_zip_") as td:
        tmp_dir = Path(td)
        extracted = []
        tmp_taken = set()      # 临时目录内已占用的名字：zip 内 a/x.txt 与
                               # b/x.txt 是两个不同文件，扁平化时不能互相覆盖
        total_bytes = 0        # 全包解压量累计（防海量小文件把 500MB 包撑到 TB 级）
        with zipfile.ZipFile(zip_path) as zf:
            if len(zf.infolist()) > _MAX_EXTRACT_FILES:
                raise ValueError(f"压缩包条目数超过上限（{_MAX_EXTRACT_FILES}）")
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
                # 拒绝符号链接条目（extract 会把链接目标写成普通文件内容）
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"拒绝符号链接条目: {info.filename!r}")
                base = name.split("/")[-1]
                # 拒绝 Windows 设备名（CON/NUL/COM1…，含 "CON.exe" 形式）
                if base.upper().split(".")[0] in _RESERVED:
                    raise ValueError(f"拒绝设备名条目: {info.filename!r}")
                if info.file_size > _MAX_EXTRACT_BYTES:
                    raise ValueError(f"拒绝超大条目: {info.filename!r}")
                # CWE-22 防护：仅取条目文件名并经 sanitize_component 净化
                # （去分隔符/../非法字符/保留名，限长），拼接到临时解压根目录，
                # 目标路径必然位于其内，不存在路径穿越面
                fname = sanitize_component(base)
                if fname in tmp_taken:
                    fname = unique_component(fname, tmp_taken)
                tmp_taken.add(fname)
                target = tmp_dir / fname
                # 流式解压（CWE-409 zip 炸弹防护的关键）：头部声明的
                # file_size 可被伪造，必须边读边累计、超限立即中断——
                # 旧实现 zf.read(info) 先整条读入内存再复核，防护形同虚设。
                # 读侧流式 + 条目 64MB 上限意味着内存峰值有界，
                # 超限在解压过程中途就中止（不会把 10GB 伪造条目解完）
                data = bytearray()
                with zf.open(info) as fsrc:
                    while True:
                        block = fsrc.read(1024 * 1024)
                        if not block:
                            break
                        data.extend(block)
                        if len(data) > _MAX_EXTRACT_BYTES:
                            raise ValueError(f"条目实际大小超限: {info.filename!r}")
                        if total_bytes + len(data) > _MAX_EXTRACT_TOTAL:
                            raise ValueError("压缩包总解压量超过上限（512MB），已中止")
                total_bytes += len(data)
                target.write_bytes(data)
                extracted.append(target)
        # 全部条目解压成功后才迁入最终目录；同名不覆盖，唯一化改名
        moved = []
        taken = {p.name for p in dest_dir.iterdir()}
        for src in extracted:
            target = dest_dir / src.name
            if src.name in taken:
                target = dest_dir / unique_component(src.name, taken)
            shutil.move(str(src), str(target))
            taken.add(target.name)
            moved.append(target)
        return moved


def _find_rar_tool():
    """探测可解 RAR 的外部工具，返回可执行文件 Path 或 None。

    顺序：7-Zip（支持 solid RAR，实测 tar 不支持）→ WinRAR/UnRAR →
    bsdtar 兜底（仅非 solid）。不打包第三方二进制：用用户机器上已有的
    解压工具，找不到时调用方降级提示手动解压。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [
        Path(pf) / "7-Zip" / "7z.exe",
        Path(pf86) / "7-Zip" / "7z.exe",
        Path(pf) / "WinRAR" / "UnRAR.exe",
        Path(pf86) / "WinRAR" / "UnRAR.exe",
        Path(pf) / "WinRAR" / "WinRAR.exe",
        Path(pf86) / "WinRAR" / "WinRAR.exe",
        Path(os.environ.get("SystemRoot") or r"C:\Windows")
        / "System32" / "tar.exe",
    ]
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    for name in ("7z", "unrar"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def safe_extract_rar(rar_path, dest_dir) -> list:
    """安全解压 RAR（用系统已有解压工具：7-Zip → WinRAR/UnRAR → bsdtar）。

    与 safe_extract_zip 同一安全契约：条目扁平化到 dest_dir、唯一化改名
    绝不覆盖、拒绝越界/绝对路径/设备名/ADS/符号链接。
    RAR 无法流式读取，防护分两层：解压前**预枚举**（只读头部：路径合法性、
    条目数、7z 还能拿到声明大小）+ 解压后严格复检（realpath 越界 / 单文件
    与总量上限 / 符号链接）。
    实测 Windows 自带 bsdtar 不支持 solid RAR（需 7-Zip）；
    找不到任何工具或解压失败抛 ValueError（调用方降级提示手动解压）。
    返回解压出的文件绝对路径列表。"""
    rar_path = Path(rar_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tool = _find_rar_tool()
    if tool is None:
        raise ValueError("未找到可解压 RAR 的工具——请安装 7-Zip 或 WinRAR，"
                         "也可用对话框「复制下载链接」手动下载解压后添加")
    with tempfile.TemporaryDirectory(prefix="th_rar_") as td:
        tmp_dir = Path(td)
        # 解压前预枚举（只读头部，不落盘）：路径合法性 + 条目数 + 声明大小。
        # zip 是流式解压天然有界；RAR 必须先整包落盘，若不预检，炸弹包会
        # 先撑满磁盘再被事后复检拒绝（2026-09-13 审查 P2-1/P2-2）
        name = tool.name.lower()
        # windowed 打包下子进程不加 CREATE_NO_WINDOW 会在桌面闪控制台窗口
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if name in ("7z.exe", "7za.exe"):
                p_list = subprocess.run(
                    [str(tool), "l", "-ba", "-slt", str(rar_path)],
                    capture_output=True, timeout=120,
                    creationflags=_NO_WINDOW)
            elif name in ("winrar.exe", "unrar.exe"):
                p_list = subprocess.run(
                    [str(tool), "lb", str(rar_path)],
                    capture_output=True, timeout=120,
                    creationflags=_NO_WINDOW)
            else:
                p_list = subprocess.run(
                    [str(tool), "-tf", str(rar_path)],
                    capture_output=True, timeout=120,
                    creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"RAR 预检失败: {e}") from e
        if p_list.returncode != 0:
            err = (p_list.stderr or p_list.stdout or b"").decode(
                "utf-8", "replace")[:200]
            raise ValueError(f"RAR 无法读取（{tool.name} 退出码 "
                             f"{p_list.returncode}）：{err}")
        # 名字列表：7z -slt 是键值行（只取 Path/Size，其余键值不能混入名字
        # 列表——含 " = " 的**文件名**只能在 unrar/tar 分支出现）；unrar lb /
        # tar -tf 一行一个名字。原先统一的 " = not in s" 豁免会让 unrar/tar
        # 分支的 "..\.. = x" 型越界条目绕过预检（2026-09-13 审查 P3）
        listed, declared_total = [], 0
        is_7z = name in ("7z.exe", "7za.exe")
        for line in (p_list.stdout or b"").decode(
                "utf-8", "replace").splitlines():
            s = line.strip()
            if not s:
                continue
            if is_7z:
                if s.startswith("Path = "):
                    listed.append(s[7:])
                elif s.startswith("Size = "):
                    try:
                        declared_total += int(s[7:])
                    except ValueError:
                        pass
            else:
                listed.append(s)
        if not listed:
            raise ValueError("RAR 内没有文件")
        if len(listed) > _MAX_EXTRACT_FILES:
            raise ValueError(f"压缩包条目数超过上限（{_MAX_EXTRACT_FILES}）")
        if declared_total > _MAX_EXTRACT_TOTAL:
            raise ValueError("压缩包声明解压量超过上限（512MB），已拒绝")
        for member in listed:
            n = member.replace("\\", "/")
            if n.startswith("/") or re.match(r"^[a-zA-Z]:", n):
                raise ValueError(f"拒绝非法压缩条目: {member!r}")
            if ".." in n.split("/"):
                raise ValueError(f"拒绝越界条目: {member!r}")
            if ":" in n:
                raise ValueError(f"拒绝非法压缩条目: {member!r}")
            if n.split("/")[-1].upper().split(".")[0] in _RESERVED:
                raise ValueError(f"拒绝设备名条目: {member!r}")
        # 解压到临时目录：各工具命令均为参数列表字面量，不经 shell 拼接
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if name in ("7z.exe", "7za.exe"):
                proc = subprocess.run(
                    [str(tool), "x", "-y", f"-o{tmp_dir}", str(rar_path)],
                    capture_output=True, timeout=600,
                    creationflags=_NO_WINDOW)
            elif name == "winrar.exe":
                proc = subprocess.run(
                    [str(tool), "x", "-ibck", "-y", str(rar_path), str(tmp_dir)],
                    capture_output=True, timeout=600,
                    creationflags=_NO_WINDOW)
            elif name == "unrar.exe":
                proc = subprocess.run(
                    [str(tool), "x", "-y", str(rar_path), str(tmp_dir)],
                    capture_output=True, timeout=600,
                    creationflags=_NO_WINDOW)
            else:
                # bsdtar 兜底：不支持 solid RAR（在此报错并提示装 7-Zip）
                proc = subprocess.run(
                    [str(tool), "-xf", str(rar_path), "-C", str(tmp_dir)],
                    capture_output=True, timeout=600,
                    creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"RAR 解压失败: {e}") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode(
                "utf-8", "replace")[:200]
            if "solid" in err.lower():
                err += "（系统 tar 不支持 solid RAR 归档，安装 7-Zip 可解）"
            raise ValueError(
                f"RAR 解压失败（{tool.name} 退出码 {proc.returncode}）：{err}")
        # 3) 解压后复检：realpath 越界 / 大小上限 / 条目数 / 符号链接
        root = tmp_dir.resolve()
        total = 0
        files = []
        for p in sorted(tmp_dir.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            real = p.resolve()
            if root not in real.parents:
                raise ValueError("RAR 含越界路径，已拒绝")
            size = p.stat().st_size
            if size > _MAX_EXTRACT_BYTES:
                raise ValueError(f"拒绝超大条目: {p.name!r}")
            total += size
            if total > _MAX_EXTRACT_TOTAL:
                raise ValueError("压缩包总解压量超过上限（512MB），已中止")
            files.append(p)
        if len(files) > _MAX_EXTRACT_FILES:
            raise ValueError(f"压缩包条目数超过上限（{_MAX_EXTRACT_FILES}）")
        # 4) 唯一化迁入（不覆盖，语义与 safe_extract_zip 一致）
        moved = []
        taken = {q.name for q in dest_dir.iterdir()}
        for src in files:
            fname = sanitize_component(src.name)
            target = dest_dir / fname
            if fname in taken:
                target = dest_dir / unique_component(fname, taken)
            shutil.move(str(src), str(target))
            taken.add(target.name)
            moved.append(target)
        return moved

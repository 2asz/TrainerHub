"""下载器基类：流式下载（进度/超时/断点续传）+ 安全校验链。"""
import os
import shutil
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urljoin

import requests

from ..audit import get_logger
from ..security import sha256_file, safe_extract_zip, safe_extract_rar, url_allowed, BROWSER_UA

_log = get_logger()


def _looks_like_exe(path) -> bool:
    """按 PE 文件头（前两字节 'MZ'）判断，不依赖扩展名（下载临时文件多为 .bin）。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def _looks_like_rar(path) -> bool:
    """按 RAR 魔数判断：RAR4 = 'Rar!\\x1a\\x07\\x00'，RAR5 = 'Rar!\\x1a\\x07\\x01\\x00'。
    下载临时文件无扩展名，只能按内容识别（小幸源部分条目为 .rar）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head.startswith(b"Rar!\x1a\x07")
# Cloudflare 友好：完整浏览器头（实测缺 sec-ch-ua 会被 403）
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 注意：不在这里放 Referer——session 级共享会把它带给所有域名的请求，
    # 请求 MediaFire 小幸分享页时因 Referer 是风灵域名被 403（2026-09-13 实测）。
    # Referer 由 _referer_for() 按目标域名逐请求设置（同域引用）。
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}
_CHUNK = 1024 * 256
_MAX_SIZE_MB = 500


def _referer_for(url: str) -> str:
    """按目标域名生成同域 Referer（源站反爬常校验 Referer 来源域）。"""
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc
    except Exception:
        netloc = ""
    return f"https://{netloc}/" if netloc else ""


class DownloadError(Exception):
    pass


class Downloader:
    """共享 Session 的流式下载器，支持断点续传（服务器支持时）。"""

    def __init__(self):
        self._session = None
        self._lock = threading.Lock()

    def _get_session(self):
        with self._lock:
            if self._session is None:
                self._session = requests.Session()
                # 忽略系统代理环境变量：代理会让 url_allowed 的私网 IP 校验
                # 被整个绕过（连接由代理发起，目标不受本进程解析约束）
                self._session.trust_env = False
                self._session.headers.update(BROWSER_HEADERS)
            return self._session

    def close(self):
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None

    def download(self, url, dest: Path, progress_cb=None, cancel=None,
                 max_size_mb=_MAX_SIZE_MB, _redo=0, allowed_hosts=None) -> Path:
        """流式下载到 dest（临时文件 + 原子替换）。返回最终路径。
        手动跟随重定向，每一跳都校验白名单域名（防止重定向到第三方/恶意域名）。
        allowed_hosts：本源下载域白名单（None = security.ALLOWED_HOSTS）。
        progress_cb(downloaded_bytes, total_bytes)；cancel: threading.Event。
        _redo：续传状态异常时删除 .part 重来的次数（内部用，防服务端行为
        异常导致无限递归）。"""
        if not url_allowed(url, allowed_hosts):
            raise DownloadError(f"下载地址不在白名单内，已拒绝: {url}")
        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DownloadError(f"无法创建下载目录: {e}") from e
        tmp = dest.with_suffix(dest.suffix + ".part")

        session = self._get_session()
        try:
            try:
                downloaded = tmp.stat().st_size if tmp.exists() else 0
            except OSError:
                # .part 被杀软锁定/竞态删除：当作从零开始
                downloaded = 0
            current = url
            for _hop in range(6):          # 最多跟随 5 跳，防重定向环
                headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
                headers["Referer"] = _referer_for(current)   # 同域引用（见 _referer_for）
                with session.get(current, stream=True, timeout=(12, 60),
                                 headers=headers, verify=True,
                                 allow_redirects=False) as r:
                    # 重定向：手动跟随并逐跳校验域名
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            raise DownloadError("重定向响应缺少 Location 头")
                        nxt = urljoin(current, loc)
                        if not url_allowed(nxt, allowed_hosts):
                            raise DownloadError(
                                f"重定向到非白名单域名，已拒绝: {nxt}")
                        current = nxt
                        continue
                    if r.status_code == 416:   # 已完整（或服务端异常）
                        # 与 206 分支同款防无限递归：异常服务器对无 Range
                        # 请求也回 416 时，重试两次后终止
                        if _redo >= 2:
                            raise DownloadError(
                                "续传状态异常（多次 416），已终止")
                        tmp.unlink(missing_ok=True)
                        return self.download(url, dest, progress_cb, cancel,
                                             max_size_mb, _redo=_redo + 1,
                                             allowed_hosts=allowed_hosts)
                    if r.status_code == 200:
                        # 服务器忽略 Range 从头返回：截断已下载的 .part，
                        # 防止"旧内容+新内容"拼接成损坏文件
                        tmp.unlink(missing_ok=True)
                        downloaded = 0
                    elif r.status_code == 206:
                        # 续传必须核对服务器确实从我们请求的偏移开始返回，
                        # 否则（服务端文件已更新/残留 .part 错位）会拼出损坏文件。
                        # 头缺失/畸形视为不支持续传：删 .part 从头重下
                        cr = r.headers.get("Content-Range") or ""
                        try:
                            start = int(str(cr).split(" ")[-1].split("/")[0]
                                        .split("-")[0])
                        except (ValueError, IndexError):
                            start = -1
                        if start != downloaded:
                            if _redo >= 2:
                                raise DownloadError(
                                    "续传状态异常（服务端多次返回错误起点），已终止")
                            _log.warning("续传起点不匹配 server=%s local=%s，"
                                         "删除 .part 从头下载（第 %s 次）",
                                         start, downloaded, _redo + 1)
                            tmp.unlink(missing_ok=True)
                            return self.download(url, dest, progress_cb, cancel,
                                                 max_size_mb, _redo=_redo + 1,
                                                 allowed_hosts=allowed_hosts)
                    else:
                        raise DownloadError(f"HTTP {r.status_code}")
                    size_limit = max_size_mb * 1024 * 1024
                    try:
                        total = downloaded + int(r.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        total = 0     # 分块传输等无/坏 Content-Length：按实际累计限流
                    if total > size_limit:
                        raise DownloadError(f"文件过大（>{max_size_mb}MB），已终止")
                    try:
                        with open(tmp, "ab") as f:
                            for block in r.iter_content(_CHUNK):
                                if cancel is not None and cancel.is_set():
                                    raise DownloadError("已取消")
                                downloaded += len(block)
                                # 分块传输没有 Content-Length，按实际累计字节限制，
                                # 防止无限写入（已在限制后截断/回滚由上层临时目录兜底）
                                if downloaded > size_limit:
                                    raise DownloadError(
                                        f"文件过大（>{max_size_mb}MB），已终止")
                                f.write(block)
                                if progress_cb:
                                    progress_cb(downloaded, total)
                    except OSError as e:
                        # OSError 捕获只包写盘段：包住整个循环会把裸 ssl.SSLError
                        # （OSError 子类）误报成"本地写入失败"误导排查
                        raise DownloadError(
                            f"本地写入失败（文件可能被占用）: {e}") from e
                    try:
                        tmp.replace(dest)
                    except OSError as e:
                        raise DownloadError(
                            f"本地写入失败（文件可能被占用）: {e}") from e
                    return dest
            raise DownloadError("重定向次数过多")
        except requests.RequestException as e:
            raise DownloadError(f"网络错误: {e}") from e

    def fetch_page(self, url, timeout=20, allowed_hosts=None) -> str:
        """拉取页面文本：白名单校验，手动跟随重定向并逐跳校验域名
        （与 download() 一致，防止中间跳转被带到非白名单域名）。
        allowed_hosts：本源页面域白名单（None = security.ALLOWED_HOSTS）。"""
        if not url_allowed(url, allowed_hosts):
            raise DownloadError(f"地址不在白名单内: {url}")
        session = self._get_session()
        current = url
        try:
            for _hop in range(6):          # 最多跟随 5 跳，防重定向环
                with session.get(current, timeout=timeout, verify=True,
                                 allow_redirects=False,
                                 headers={"Referer": _referer_for(current)}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            raise DownloadError("重定向响应缺少 Location 头")
                        nxt = urljoin(current, loc)
                        if not url_allowed(nxt, allowed_hosts):
                            raise DownloadError(
                                f"重定向到非白名单域名，已拒绝: {nxt}")
                        current = nxt
                        continue
                    r.raise_for_status()
                    # 无 charset 声明时 requests 默认 ISO-8859-1，中文名乱码
                    if not r.encoding or r.encoding.lower() == "iso-8859-1":
                        r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
            raise DownloadError("重定向次数过多")
        except requests.RequestException as e:
            raise DownloadError(f"网络错误: {e}") from e


class TrainerDownloader:
    """官网适配器基类。

    子类通过类属性 ALLOWED_HOSTS 自带下载域白名单（新增源 = 新子类自带
    域名，无需改全局配置）；未覆盖时回退 security.ALLOWED_HOSTS。
    白名单的实际执行在 Downloader.download/fetch_page（参数传入）。"""

    SOURCE = "本地"     # 基类兜底（子类应覆盖为自己的来源名）
    ALLOWED_HOSTS = None    # None → security.ALLOWED_HOSTS
    # 站点标题为英文时置 True（如风灵源）：调用方据此对中文名游戏做
    # 「查 Steam 英文名 → 重试搜索」的补偿。基类固化语义，避免隐式契约
    NEEDS_ENGLISH_NAME = False

    def __init__(self, downloader: Downloader):
        self._dl = downloader

    def url_allowed(self, url) -> bool:
        from ..security import ALLOWED_HOSTS, url_allowed as _check
        return _check(url, self.ALLOWED_HOSTS or ALLOWED_HOSTS)

    def fetch_page(self, url, timeout=20) -> str:
        """按本源白名单拉取页面文本（子类统一入口）。"""
        return self._dl.fetch_page(url, timeout=timeout,
                                   allowed_hosts=self.ALLOWED_HOSTS)

    def search(self, query: str) -> list:
        raise NotImplementedError

    def resolve_downloads(self, page_url: str) -> list:
        """解析下载页，返回按版本号降序的 [{url, version, name}]。"""
        raise NotImplementedError

    def manual_links(self, page_url: str) -> list:
        """自动直链解析失败时的**手动下载渠道**（网盘链接等，仅可复制）。
        默认无；有网盘渠道的源（如小幸）覆盖。返回 [{name, url}]。"""
        return []

    def install(self, game_name: str, page_url: str, dest_root: Path,
                progress_cb=None, cancel=None, entry=None) -> dict:
        """完整流程：解析直链 → 下载 → 校验 → 入库（exe 直用 / zip 解压）。
        entry: 用户选定的版本条目 {url, version, name}（None=自动取最新）。
        返回 {"exe_path", "dir_path", "sha256", "version", "url"}。"""
        if entry is not None:
            entry = dict(entry)         # 跳过解析，直接用用户选定的版本
        else:
            entries = self.resolve_downloads(page_url)
            if not entries:
                raise DownloadError(
                    "未能从页面解析出下载链接（网盘文件可能已被删除，"
                    "或网盘临时限流——可稍后重试，或复制页面链接到浏览器确认）")
            entry = entries[0]          # 最新版本
        url = entry["url"]
        version = entry.get("version", "")
        if not self.url_allowed(url):
            raise DownloadError(f"下载地址不在白名单内，已拒绝: {url}")

        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="th_dl_") as td:
            dl_path = Path(td) / "download.bin"
            self._dl.download(url, dl_path, progress_cb, cancel,
                              allowed_hosts=self.ALLOWED_HOSTS)
            sha = sha256_file(dl_path)

            if _looks_like_exe(dl_path):
                # 直连 exe：按原始文件名入库。先落 .downloading 暂存名再
                # os.replace 原子覆盖——此前"先 unlink 旧文件再 move"，
                # move 中途失败（跨卷磁盘满/杀软占用）会两头落空
                fname = sanitize_dest_name(entry.get("name") or dl_path.name)
                target = dest_root / fname
                staging = target.with_name(target.name + ".downloading")
                shutil.move(str(dl_path), str(staging))
                try:
                    os.replace(str(staging), str(target))
                except OSError as e:
                    try:
                        staging.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise DownloadError(
                        f"目标文件被占用，无法覆盖：{target}") from e
                return {"exe_path": str(target), "dir_path": str(dest_root),
                        "sha256": sha, "version": version, "url": url}

            if zipfile.is_zipfile(dl_path):
                files = []
                try:
                    files = safe_extract_zip(dl_path, dest_root)
                    return self._pick_extracted_exe(files, dest_root, sha,
                                                    version, url)
                except DownloadError:
                    self._cleanup_extracted(files)
                    raise
                except (ValueError, OSError, zipfile.BadZipFile, zlib.error) as e:
                    # 解压失败/包内无 exe/杀软隔离 exe 等全部归一为业务异常，
                    # 且清理本次已迁入的产物——残留的 exe 会被 scanner 再捞起、
                    # 重试时 unique 改名越积越多（2026-09-13 审查 P2）
                    self._cleanup_extracted(files)
                    raise DownloadError(f"压缩包解压失败：{e}") from e

            if _looks_like_rar(dl_path):
                # 小幸源部分条目为 RAR（实测「战神4」），走系统 bsdtar 解压
                files = []
                try:
                    files = safe_extract_rar(dl_path, dest_root)
                    return self._pick_extracted_exe(files, dest_root, sha,
                                                    version, url)
                except DownloadError:
                    self._cleanup_extracted(files)
                    raise
                except ValueError as e:
                    self._cleanup_extracted(files)
                    # 归一为业务异常：调用方（对话框）按 DownloadError 处理
                    raise DownloadError(str(e)) from e
                except OSError as e:
                    self._cleanup_extracted(files)
                    raise DownloadError(f"RAR 解压失败：{e}") from e

            raise DownloadError(
                "下载的文件既不是 EXE 也不是 ZIP/RAR（可能被重定向到第三方网盘）。"
                "请尝试手动下载后添加。")

    @staticmethod
    def _cleanup_extracted(files) -> None:
        """删除本次解压已迁入的产物（失败路径的垃圾清理）。files 里的
        路径都是唯一化改名后的新文件，不会误删 dest_root 里的旧文件。"""
        for f in files or []:
            try:
                Path(f).unlink()
            except OSError:
                pass

    @staticmethod
    def _pick_extracted_exe(files, dest_root, sha, version, url) -> dict:
        """从解压结果挑主程序：取体积最大的 exe（多 exe 包常见 helper 干扰）。
        无 exe 抛 DownloadError。"""
        exes = [f for f in files if Path(f).suffix.lower() == ".exe"]
        if not exes:
            raise DownloadError("压缩包内未找到可执行文件")
        exe = max(exes, key=lambda f: Path(f).stat().st_size)
        return {"exe_path": str(exe), "dir_path": str(dest_root),
                "sha256": sha, "version": version, "url": url}


def sanitize_dest_name(name: str) -> str:
    """净化下载目标文件名，防路径注入。"""
    from ..security import sanitize_component
    name = sanitize_component(name)
    if not name.lower().endswith(".exe"):
        name += ".exe"
    return name

"""下载器基类：流式下载（进度/超时/断点续传）+ 安全校验链。"""
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

from ..audit import get_logger
from ..security import sha256_file, safe_extract_zip, url_allowed

_log = get_logger()


def _looks_like_exe(path) -> bool:
    """按 PE 文件头（前两字节 'MZ'）判断，不依赖扩展名（下载临时文件多为 .bin）。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False
# Cloudflare 友好：完整浏览器头（实测缺 sec-ch-ua 会被 403）
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://flingtrainer.com/",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}
_CHUNK = 1024 * 256
_MAX_SIZE_MB = 500


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
                 max_size_mb=_MAX_SIZE_MB) -> Path:
        """流式下载到 dest（临时文件 + 原子替换）。返回最终路径。
        手动跟随重定向，每一跳都校验白名单域名（防止重定向到第三方/恶意域名）。
        progress_cb(downloaded_bytes, total_bytes)；cancel: threading.Event。"""
        if not url_allowed(url):
            raise DownloadError(f"下载地址不在白名单内，已拒绝: {url}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")

        session = self._get_session()
        try:
            downloaded = tmp.stat().st_size if tmp.exists() else 0
            current = url
            for _hop in range(6):          # 最多跟随 5 跳，防重定向环
                headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
                with session.get(current, stream=True, timeout=(12, 60),
                                 headers=headers, verify=True,
                                 allow_redirects=False) as r:
                    # 重定向：手动跟随并逐跳校验域名
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            raise DownloadError("重定向响应缺少 Location 头")
                        nxt = urljoin(current, loc)
                        if not url_allowed(nxt):
                            raise DownloadError(
                                f"重定向到非白名单域名，已拒绝: {nxt}")
                        current = nxt
                        continue
                    if r.status_code == 416:   # 已完整
                        tmp.unlink(missing_ok=True)
                        return self.download(url, dest, progress_cb, cancel,
                                             max_size_mb)
                    if r.status_code == 200:
                        # 服务器忽略 Range 从头返回：截断已下载的 .part，
                        # 防止"旧内容+新内容"拼接成损坏文件
                        tmp.unlink(missing_ok=True)
                        downloaded = 0
                    elif r.status_code != 206:
                        raise DownloadError(f"HTTP {r.status_code}")
                    size_limit = max_size_mb * 1024 * 1024
                    total = downloaded + int(r.headers.get("Content-Length") or 0)
                    if total > size_limit:
                        raise DownloadError(f"文件过大（>{max_size_mb}MB），已终止")
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
                    tmp.replace(dest)
                    return dest
            raise DownloadError("重定向次数过多")
        except requests.RequestException as e:
            raise DownloadError(f"网络错误: {e}") from e

    def fetch_page(self, url, timeout=20) -> str:
        """拉取页面文本：白名单校验，手动跟随重定向并逐跳校验域名
        （与 download() 一致，防止中间跳转被带到非白名单域名）。"""
        if not url_allowed(url):
            raise DownloadError(f"地址不在白名单内: {url}")
        session = self._get_session()
        current = url
        try:
            for _hop in range(6):          # 最多跟随 5 跳，防重定向环
                with session.get(current, timeout=timeout, verify=True,
                                 allow_redirects=False) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            raise DownloadError("重定向响应缺少 Location 头")
                        nxt = urljoin(current, loc)
                        if not url_allowed(nxt):
                            raise DownloadError(
                                f"重定向到非白名单域名，已拒绝: {nxt}")
                        current = nxt
                        continue
                    r.raise_for_status()
                    return r.text
            raise DownloadError("重定向次数过多")
        except requests.RequestException as e:
            raise DownloadError(f"网络错误: {e}") from e


class TrainerDownloader:
    """官网适配器基类。"""

    SOURCE = "本地"     # 基类兜底（子类应覆盖为自己的来源名）

    def __init__(self, downloader: Downloader):
        self._dl = downloader

    def search(self, query: str) -> list:
        raise NotImplementedError

    def resolve_downloads(self, page_url: str) -> list:
        """解析下载页，返回按版本号降序的 [{url, version, name}]。"""
        raise NotImplementedError

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
                raise DownloadError("未能从页面解析出下载链接")
            entry = entries[0]          # 最新版本
        url = entry["url"]
        version = entry.get("version", "")

        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="th_dl_") as td:
            dl_path = Path(td) / "download.bin"
            self._dl.download(url, dl_path, progress_cb, cancel)
            sha = sha256_file(dl_path)

            if _looks_like_exe(dl_path):
                # 直连 exe：直接入库（按原始文件名，防重名加后缀）
                fname = sanitize_dest_name(entry.get("name") or dl_path.name)
                target = dest_root / fname
                # 临时目录与修改器目录可能不在同一磁盘，Path.replace 会抛跨卷错误，
                # 用 shutil.move（同卷走 rename，跨卷复制+删除）
                shutil.move(str(dl_path), str(target))
                return {"exe_path": str(target), "dir_path": str(dest_root),
                        "sha256": sha, "version": version, "url": url}

            if zipfile.is_zipfile(dl_path):
                files = safe_extract_zip(dl_path, dest_root)
                exes = [f for f in files if f.suffix.lower() == ".exe"]
                if not exes:
                    raise DownloadError("压缩包内未找到可执行文件")
                exe = max(exes, key=lambda f: f.stat().st_size)
                return {"exe_path": str(exe), "dir_path": str(dest_root),
                        "sha256": sha, "version": version, "url": url}

            raise DownloadError(
                "下载的文件既不是 EXE 也不是 ZIP（可能被重定向到第三方网盘）。"
                "请尝试手动下载后添加。")


def sanitize_dest_name(name: str) -> str:
    """净化下载目标文件名，防路径注入。"""
    from ..security import sanitize_component
    name = sanitize_component(name)
    if not name.lower().endswith(".exe"):
        name += ".exe"
    return name

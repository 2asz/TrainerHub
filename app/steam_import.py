"""Steam 游戏导入：
1) 解析桌面 .url/.lnk 快捷方式，提取 steam://rungameid/<id> 或 Steam AppID；
2) 调 Steam 官方 API（免费免 key）取中文名 + 封面，带重试退避 + 磁盘缓存。"""
import json
import re
import threading
import time
from pathlib import Path

import requests

from .config import DATA_DIR
from .audit import get_logger

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_STEAM_RUN_RE = re.compile(r"steam://rungameid/(\d+)")
_EPIC_RE = re.compile(r"com\.epicgames\.launcher://")
_STORE_CC = "cn"
_APPINFO_CACHE = DATA_DIR / "steam_appinfo_cache.json"
_CACHE_TTL = 7 * 86400  # 7 天
_log = get_logger()


def parse_shortcut(path) -> dict | None:
    """解析 .url 网络快捷方式，返回 {appid} 或 {launch_cmd} 或 None。"""
    p = Path(path)
    try:
        if p.suffix.lower() == ".url":
            text = p.read_text(encoding="utf-8", errors="ignore")
        elif p.suffix.lower() == ".lnk":
            # .lnk 为二进制，仅当内部含 steam:// 文本时提取（极少见）
            text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            return None
    except OSError:
        return None

    m = _STEAM_RUN_RE.search(text)
    if m:
        return {"appid": m.group(1)}
    # Epic 商店游戏：com.epicgames.launcher://apps/<id>，整段协议作为启动值
    if _EPIC_RE.search(text):
        m = re.search(r"URL\s*=\s*(com\.epicgames\.launcher://[^\s]+)", text, re.I)
        if m:
            return {"launch": {"type": "epic", "value": m.group(1)}}
    # 非 Steam 的 .lnk：解析目标路径与参数（分离存储，launcher 参数化启动，
    # 避免"路径+参数"拼成整串被当作文件路径检查）
    if p.suffix.lower() == ".lnk":
        resolved = _resolve_lnk_target(p)
        if resolved:
            target, args = resolved
            launch = {"type": "file", "value": str(target)}
            if args:
                launch["args"] = args
            return {"launch": launch}
    return None


def _resolve_lnk_target(lnk_path):
    """用 powershell 解析 .lnk 快捷方式目标与参数（只读操作，慢但准确）。
    返回 (目标路径, 参数) 元组；失败返回 None。
    注意：PowerShell 单引号字符串不做转义，分隔符须用 [char]9（真正的制表符）。
    路径单引号用 '' 转义，防命令注入。"""
    import subprocess
    q = str(lnk_path).replace("'", "''")
    try:
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s').TargetPath; "
              "$a=(New-Object -ComObject WScript.Shell).CreateShortcut('%s').Arguments; "
              "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Write-Output ($s + [char]9 + $a)" % (q, q))
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             encoding="utf-8", errors="replace")
        parts = out.stdout.strip().split("\t", 1)
        if parts and parts[0]:
            return parts[0], (parts[1] if len(parts) > 1 else "")
    except Exception:
        pass
    return None


# ---------------- Steam AppInfo API（缓存 + 重试退避） ----------------
def search_steam_appid(query, timeout=12, cancel=None) -> list:
    """Steam 官方商店搜索：按游戏名查 AppID。
    返回 [{appid, name, cover_url}]；失败/无结果返回空列表。
    带失败退避与空结果回退（cc=cn 国区搜不到时回退 cc=us）。
    请求经 safe_get 逐跳校验域名与 IP 边界（防 SSRF）。
    cancel: threading.Event，置位时停止（对话框关闭等场景）。"""
    import urllib.parse
    from .security import STEAM_API_HOSTS, safe_get
    q = urllib.parse.quote(query.strip())
    for cc in ("cn", "us"):
        if cancel is not None and cancel.is_set():
            return []
        url = (f"https://store.steampowered.com/api/storesearch/"
               f"?term={q}&cc={cc}&l=schinese")
        body = safe_get(url, STEAM_API_HOSTS, timeout=timeout, max_hops=2,
                        headers={"User-Agent": _UA})
        if not body:
            continue
        try:
            items = json.loads(body).get("items", [])
        except ValueError:
            continue
        if items:
            return [{"appid": str(i.get("id")),
                     "name": i.get("name", ""),
                     "cover_url": i.get("tiny_image") or ""}
                    for i in items]
    return []


class SteamAppInfo:
    """按需拉取 appinfo，磁盘缓存 7 天，线程池内调用。
    写入节流：批量导入时最多每 _SAVE_THROTTLE 秒落盘一次，close() 时强制 flush。"""

    _SAVE_THROTTLE = 5.0

    def __init__(self):
        self._cache = {}
        self._lock = threading.RLock()
        self._session = None
        self._dirty = False
        self._last_save = 0.0
        self._load_cache()

    def _load_cache(self):
        try:
            if _APPINFO_CACHE.exists():
                raw = json.loads(_APPINFO_CACHE.read_text(encoding="utf-8"))
                if raw.get("ts", 0) > time.time() - _CACHE_TTL:
                    self._cache = raw.get("data", {})
        except Exception:
            self._cache = {}

    def _save_cache(self, force=False):
        """节流写盘；force=True 时立即写（close/退出场景）。
        写失败保留脏标记，下次写入时重试，不静默丢数据。"""
        if not self._dirty and not force:
            return
        now = time.time()
        if not force and now - self._last_save < self._SAVE_THROTTLE:
            return
        self._dirty = False
        self._last_save = now
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _APPINFO_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "data": self._cache},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(_APPINFO_CACHE)
        except Exception:
            self._dirty = True     # 写失败：保留脏标记供下次重试

    def _session_get(self):
        with self._lock:
            if self._session is None:
                self._session = requests.Session()
                self._session.headers.update({"User-Agent": _UA})
            return self._session

    def fetch(self, appid, cancel=None) -> dict | None:
        """返回 {name, cover_url} 或 None。

        先查磁盘缓存；未命中则按 cc=cn → cc=us 依次请求 Steam appdetails
        （和商店搜索的回退逻辑一致——部分游戏国区锁区/未上架，us 区能拿到封面）。
        cn/us 都"确定无数据"时才把 None 写缓存（防反复请求）；
        网络错误不写缓存，下次会自动重试。
        cancel: threading.Event，置位时停止（关闭窗口等场景）。"""
        appid = str(appid)
        with self._lock:
            if appid in self._cache:
                return self._cache[appid]
        last_err = None
        confirmed_miss = False
        for cc in (_STORE_CC, "us"):
            if cancel is not None and cancel.is_set():
                return None
            url = (f"https://store.steampowered.com/api/appdetails/"
                   f"?appids={appid}&cc={cc}&l=schinese")
            try:
                r = self._session_get().get(url, timeout=12)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code} cc={cc}"
                    continue
                data = r.json().get(appid, {})
                if data.get("success") and data.get("data"):
                    d = data["data"]
                    info = {
                        "name": d.get("name"),
                        "cover_url": d.get("header_image"),
                    }
                    with self._lock:
                        self._cache[appid] = info
                        self._dirty = True
                        self._save_cache()
                    return info
                # 200 但无数据（锁区/未上架等）：记录为"确定无结果"，继续试下一个 cc
                confirmed_miss = True
                last_err = f"无数据 cc={cc}"
            except requests.RequestException as e:
                last_err = f"cc={cc}: {e}"
            except ValueError as e:
                last_err = f"cc={cc}: JSON {e}"
            if cancel is not None and cancel.is_set():
                return None
        if confirmed_miss:
            # 两个区都确定没有：缓存 None，本会话内不再反复请求
            with self._lock:
                self._cache[appid] = None
                self._dirty = True
                self._save_cache()
            return None
        if last_err:
            _log.warning("Steam appinfo 获取失败 appid=%s: %s", appid, last_err)
        return None

    def close(self):
        with self._lock:
            self._save_cache(force=True)   # flush 未落盘的缓存（批量导入节流场景）
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None

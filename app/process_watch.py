"""游戏进程检测：Toolhelp 快照 + psutil 可选校验。
- 主轮询用 Windows Toolhelp32Snapshot 一次拿全进程名（~7ms/轮，
  相比 psutil.process_iter 的 ~680ms/轮，避免每 2 秒主线程卡顿）；
- 窗口失焦/最小化时降为低频轮询（省资源，且不延迟自动启动修改器）；
- 匹配：进程名映射为主；存在纯路径条目的游戏需要精确匹配时才用
  psutil 取 exe（懒加载，仅命中名称候选的进程才查，避免全量 OpenProcess）。"""
import ctypes
import os
import time
from ctypes import wintypes

import psutil
from PySide6.QtCore import QObject, QTimer, Signal

from .config import config
from .audit import get_logger

_log = get_logger()

# 非游戏进程：平台辅助程序 + 后台常驻程序。
# 为什么需要这份名单：启动游戏瞬间的进程快照对比（auto_assoc_process）会误把
# 同时被拉起的 node/更新器/EOS 服务/Steam 叠层等记成游戏进程，而这些程序常驻后台，
# 只要它们还在跑，游戏就会"已退出却仍显示运行中"。名单内的进程一律
# 不参与匹配、不被自动关联、启动时还会从库中清理（见 prune_background_process_names）。
_NON_GAME_EXES = {
    # Steam/Epic 客户端与叠层（叠层 gameoverlayui64 在你玩任意 Steam 游戏时都在跑）
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe",
    "gameoverlayui64.exe", "vulkandriverquery64.exe",
    # Epic Online Services / EOS 引导与叠层渲染器
    "eosbootstrapper.exe", "epiconlineservicesuserhelper.exe",
    "eosoverlayrenderer-win64-shipping.exe",
    # 崩溃报告器 / 反作弊宿主
    "crashpad_handler.exe", "crashreporter.exe", "unrealcefsubprocess.exe",
    # 运行时与通用更新器（node.exe 只要开着 Node 相关工具就在跑）
    "node.exe", "updater.exe",
    # 笔记本厂商常驻软件 / Windows 游戏栏与游戏服务（GameBar 平时就在后台跑）
    "legionzone.exe",
    "gamebar.exe", "gamebarftserver.exe", "gamebarpresencewriter.exe",
    "gamingservices.exe", "gamingservicesnet.exe",
}

# ---------------- Toolhelp32Snapshot：Windows 原生快速枚举 ----------------
_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _load_kernel32():
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(_PROCESSENTRY32W)]
    return k32


try:
    _k32 = _load_kernel32()
except (AttributeError, OSError):
    _k32 = None     # 非 Windows：降级（_fast_process_names 返回空）


def _fast_process_entries() -> list:
    """Windows 快速枚举：返回 [(pid, exe名小写)]。
    实测 333 进程 ~7ms/轮（vs psutil 全量 ~680ms/轮）。失败返回空列表。"""
    if _k32 is None:
        return []
    h = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if h in (None, wintypes.HANDLE(-1).value):
        return []
    out = []
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(h, ctypes.byref(pe))
        while ok:
            out.append((pe.th32ProcessID, pe.szExeFile.casefold()))
            ok = _k32.Process32NextW(h, ctypes.byref(pe))
    finally:
        try:
            _k32.CloseHandle(h)
        except Exception:
            pass
    return out


def _fast_process_names() -> list:
    """兼容包装：仅返回进程名列表（小写）。"""
    return [name for _, name in _fast_process_entries()]


def _pn_norm(p) -> str:
    """规范化 process_names 条目用于比较：完整路径 → normcase+normpath；
    纯进程名 → 小写。空/非字符串 → 空串。"""
    if not isinstance(p, str) or not p:
        return ""
    if "\\" in p or "/" in p:
        return os.path.normcase(os.path.normpath(p))
    return p.casefold()


def _is_under(root, child) -> bool:
    """child 路径是否位于 root 目录内（Windows 盘符/大小写不敏感，
    用规范化后的前缀比较，替代 is_relative_to 的大小写敏感判断）。"""
    r = str(root).replace("\\", "/").rstrip("/").casefold()
    c = str(child).replace("\\", "/").casefold()
    return c == r or c.startswith(r + "/")


class ProcessWatch(QObject):
    running_changed = Signal(str, bool)     # game_id, is_running
    # 运行状态即将变化（先于 running_changed 发出），允许外部拦截处理
    about_to_change = Signal(str, bool)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._timer = QTimer(self)
        self._interval_focused = int(config.get("poll_interval_ms"))
        self._interval_blurred = max(self._interval_focused * 5, 10000)
        self._timer.setInterval(self._interval_focused)
        self._timer.timeout.connect(self._poll)
        self._running = set()
        self._focused = True
        # 进程名 -> game_id / exe 路径 -> game_id 双映射缓存；
        # 仅在库版本号变化时重建（不再每轮全量重建）
        self._names_to_games = {}
        self._paths_to_games = {}
        self._names_exact = {}       # 名字 → {gid: [规范化路径]}（见 _build_map）
        self._built_revision = -1

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_focused(self, focused: bool):
        """失焦/最小化时降为低频轮询而不是完全停止：
        避免自动启动修改器等依赖运行状态的功能被延迟。"""
        self._focused = bool(focused)
        self._timer.setInterval(
            self._interval_focused if focused else self._interval_blurred)
        if focused:
            self._poll()

    def force_poll(self):
        """立即重扫进程并重新判定运行状态（不等下一个定时器周期）。

        供主界面「刷新」按钮调用：_poll 会拿当前进程快照与旧运行集合做差集，
        已停止的游戏当场发出 running_changed(gid, False)，徽章立即熄灭——
        等效于"重启后马上做一次轮询"。"""
        self._poll()

    def is_running(self, game_id) -> bool:
        return game_id in self._running

    def _build_map(self):
        """重建 进程名→游戏 与 exe路径→游戏 双映射。
        revision 未变时复用缓存。"""
        rev = getattr(self._library, "revision", None)
        if rev is not None and rev == self._built_revision:
            return self._names_to_games, self._paths_to_games
        names_to_games = {}
        paths_to_games = {}
        # 名字 → {gid: [规范化完整路径]}：登记了完整路径的游戏，
        # 轮询时按 exe 路径精确核对，防止同名后台进程造成"假运行中"
        names_exact = {}
        for game in self._library.all_games():
            for pn in game.get("process_names", []):
                if not isinstance(pn, str) or not pn:
                    continue
                # 非游戏进程（node/更新器/EOS 等）不参与匹配——
                # 历史误关联数据里常有它们，参与匹配会让游戏"假运行中"
                if pn.replace("\\", "/").rsplit("/", 1)[-1].casefold() \
                        in _NON_GAME_EXES:
                    continue
                if "\\" in pn or "/" in pn:
                    base = pn.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                    paths_to_games.setdefault(
                        _pn_norm(pn), set()).add(game["id"])
                    # 路径条目同时注册 basename：Toolhelp 按名称发现候选，
                    # 匹配该名字时再按 exe 路径精确区分
                    names_to_games.setdefault(base, set()).add(game["id"])
                    names_exact.setdefault(base, {}).setdefault(
                        game["id"], []).append(_pn_norm(pn))
                else:
                    names_to_games.setdefault(
                        pn.casefold(), set()).add(game["id"])
            launch = game.get("launch") or {}
            if launch.get("type") == "file" and launch.get("value"):
                exe = str(launch["value"])
                if exe.lower().endswith(".exe"):
                    base = exe.rsplit("\\", 1)[-1].casefold()
                    names_to_games.setdefault(base, set()).add(game["id"])
                    paths_to_games.setdefault(_pn_norm(exe), set()).add(game["id"])
                    names_exact.setdefault(base, {}).setdefault(
                        game["id"], []).append(_pn_norm(exe))
        self._names_to_games = names_to_games
        self._paths_to_games = paths_to_games
        self._names_exact = names_exact
        self._built_revision = rev
        return names_to_games, paths_to_games

    def _poll(self):
        names_to_games, paths_to_games = self._build_map()
        start = time.monotonic()
        now_running = set()

        # Toolhelp 快照：一次枚举全进程 (pid, name)（~7ms），不逐进程 OpenProcess
        entries = _fast_process_entries()
        if not entries:
            # 快照失败（极少见）：退化为 psutil 名称扫描，避免漏检/状态卡住
            try:
                entries = [(int(p.info["pid"]),
                            (p.info.get("name") or "").casefold())
                           for p in psutil.process_iter(["pid", "name"])]
            except Exception:
                entries = []
        if not entries:
            return
        for pid, name in entries:
            gids = names_to_games.get(name)
            if not gids:
                continue
            exact = self._names_exact.get(name)
            exe = ""            # 本次已取得的 exe 路径，复用给 _assoc_process（省一次 psutil 查询）
            if exact:
                # 该名字下有游戏登记了完整路径：逐进程按 exe 路径精确核对。
                # 路径对不上的同名进程（后台常驻软件/其他游戏的同名 exe）
                # 一律不算——这就是"游戏退出后仍显示运行中"的根治手段
                try:
                    exe = psutil.Process(pid).exe() or ""
                except Exception:
                    exe = ""
                en = _pn_norm(exe) if exe else ""
                if en:
                    for gid, paths in exact.items():
                        if en in paths:
                            now_running.add(gid)
                # 对该名字只有纯名记录的游戏仍按名字匹配（历史数据/手工录入），
                # 但已登记路径的游戏绝不做名字通配
                for gid in gids:
                    if gid not in exact:
                        now_running.add(gid)
            else:
                now_running.update(gids)
            # auto_assoc_process：检测到运行中进程时自动补录（按 pid 取 exe 路径）
            if config.get("auto_assoc_process"):
                self._assoc_process(pid, gids, exe)

        # 轮询过慢时降频（兜底保护，正常路径不应触发）
        elapsed = time.monotonic() - start
        if elapsed > 0.3 and not getattr(self, "_slow_poll", False):
            _log.warning("进程轮询耗时 %.0fms，降低轮询频率", elapsed * 1000)
            self._slow_poll = True
            self._timer.setInterval(max(self._interval_focused * 5, 10000))
        elif elapsed <= 0.3 and getattr(self, "_slow_poll", False):
            self._slow_poll = False
            self._timer.setInterval(self._interval_focused)

        for gid in now_running - self._running:
            self._running.add(gid)
            self.about_to_change.emit(gid, True)
            self.running_changed.emit(gid, True)
        for gid in self._running - now_running:
            self._running.discard(gid)
            self.about_to_change.emit(gid, False)
            self.running_changed.emit(gid, False)

    def _assoc_process(self, pid, gids, exe=""):
        """把匹配到的运行中进程补录进游戏的 process_names（不存在才加）。

        只登记完整 exe 路径（优先复用调用方已取得的 exe，取不到再按 pid 查一次）：
        纯名条目不参与路径核对，后台同名进程会靠它伪装成游戏。
        查询不到路径（权限/进程已退出）就跳过，宁缺毋滥。
        非游戏进程（名单见 _NON_GAME_EXES）直接跳过。"""
        if not exe:
            try:
                exe = psutil.Process(pid).exe() or ""
            except Exception:
                exe = ""
        if not exe:
            return
        if exe.replace("\\", "/").rsplit("/", 1)[-1].casefold() \
                in _NON_GAME_EXES:
            return
        key = exe
        norm = _pn_norm(key)
        for gid in gids:
            game = self._library.get_game(gid)
            if not game:
                continue
            pns = list(game.get("process_names") or [])
            if any(_pn_norm(p) == norm for p in pns):
                continue
            pns.append(key)
            self._library.update_game(gid, process_names=pns)

    def capture_processes(self) -> dict:
        """返回 {pid: (exe, name)} 进程快照（仅 .exe）。
        供启动游戏前后对比：新增 PID 的 exe 路径即为该游戏的关联进程。"""
        out = {}
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    pid = proc.info["pid"]
                    exe = proc.info.get("exe") or ""
                    name = proc.info.get("name") or ""
                except (psutil.Error, OSError):
                    continue
                if not name.lower().endswith(".exe") \
                        and not exe.lower().endswith(".exe"):
                    continue
                out[pid] = (str(exe), name)
        except (psutil.Error, OSError):
            pass
        return out

    def associate_processes(self, gid, new_processes, roots=None):
        """把启动游戏后新出现的进程关联到游戏记录，保存规范化 exe 路径（优先）。
        roots：允许关联的安装根目录列表（游戏安装位置的目录）。
        不在任何安装目录内的进程一律不关联——这是防止"游戏启动瞬间把
        后台常驻软件（LegionZone/GameBar/更新器…）误记为游戏进程"的根本手段。
        roots 为空 None/[] 时不做目录限制（仅靠排除名单过滤）。
        过滤：
        - 仅 .exe 进程，跳过空进程名；
        - 排除 Windows 系统目录进程；
        - 排除已知平台辅助进程（Steam/Epic 客户端、崩溃报告器、反作弊宿主）；
        - 不在安装目录内（roots 给定且非空时）；
        - 排除已属于其他游戏或本游戏已有 及 本次已加（集合去重，同名 PID 不重复）。"""
        game = self._library.get_game(gid)
        if not game:
            return
        other_owned = set()
        for g in self._library.all_games():
            if g["id"] == gid:
                continue
            other_owned.update(_pn_norm(p) for p in g.get("process_names", []))
        existing = {_pn_norm(p) for p in game.get("process_names") or []}
        roots = roots or []
        added = []
        seen = set()
        for exe, name in new_processes:
            key = str(exe) if exe else str(name)
            if not key.strip():
                continue                        # 跳过空进程名
            low_exe = str(exe).lower() if exe else ""
            low_name = str(name or "").lower()
            if not (low_name.endswith(".exe") or low_exe.endswith(".exe")):
                continue
            if low_exe and "/windows/" in low_exe.replace("\\", "/"):
                continue
            if low_name in _NON_GAME_EXES:
                continue
            if exe and roots and not any(_is_under(r, exe) for r in roots):
                continue                        # 不在游戏安装目录内：非游戏进程
            norm = _pn_norm(key)
            if norm in existing or norm in other_owned or norm in seen:
                continue
            seen.add(norm)
            added.append(key)
        if added:
            pns = list(game.get("process_names") or []) + sorted(added)
            self._library.update_game(gid, process_names=pns)

    def prune_background_process_names(self):
        """启动清理：移除 process_names 中被误关联的后台常驻/平台进程
        （node、更新器、EOS 服务、Steam 叠层等，名单见 _NON_GAME_EXES）。

        为什么需要：启动游戏瞬间的快照对比会误把这些常驻程序记成游戏进程，
        之后只要它们还在跑（例如 node.exe 一直开着），游戏就会
        "已退出却仍显示运行中"。返回移除的条目总数。"""
        removed = 0
        for game in self._library.all_games():
            pns = game.get("process_names") or []
            kept = [p for p in pns
                    if not (isinstance(p, str)
                            and p.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                            in _NON_GAME_EXES)]
            if len(kept) != len(pns):
                self._library.update_game(game["id"], process_names=kept)
                removed += len(pns) - len(kept)
        if removed:
            _log.info("启动清理：移除 %d 个误关联的后台进程名（防假运行中）",
                      removed)
        return removed

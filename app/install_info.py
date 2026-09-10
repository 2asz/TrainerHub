"""游戏安装信息解析（Steam / Epic / 本地）与代表 exe 查找。

为什么独立出来：
- 安装目录解析原本写在 main_window.py，被「进程关联」「启动清理」「封面生成」多处需要；
- 单独成模块后，封面生成等低层逻辑不必反向导入 UI 层，避免循环引用。

新手提示：这里没有 Qt，只有 Python 标准库 + pathlib，负责"找到游戏装在哪、主程序是哪个 exe"。
"""
import json
import os
import re
import threading
import time
import winreg
from pathlib import Path

# 安装信息读取全是系统级慢操作（注册表 OpenKey×3、解析 libraryfolders.vdf、
# 逐个读 *.item JSON）。此前进程轮询每 2 秒对命中进程调 game_install_roots
# 全量重读——现在统一走 5 分钟 TTL 缓存（key 见 _cached 各调用点）
_INSTALL_INFO_TTL = 300.0
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, fn, negative_ttl=None):
    """极简 TTL 缓存：key 为 (名称, 参数) 元组，value 为 fn() 的返回值。
    negative_ttl：fn() 返回 None（未找到）时的短 TTL——新装的Steam/Epic 游戏
    清单会随时出现，负结果不能像正结果一样缓存 5 分钟。"""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            value = hit[1]
            ttl = _INSTALL_INFO_TTL if value is not None else (
                negative_ttl if negative_ttl is not None else _INSTALL_INFO_TTL)
            if now - hit[0] < ttl:
                return value
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def clear_install_info_cache():
    """清空安装信息缓存（单元测试注入假目录/清单后调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------- Steam ----------

def _steam_library_dirs() -> list:
    """Steam 所有库的 steamapps 目录：注册表找根目录 + libraryfolders.vdf 多库
    （Steam 未安装返回空列表）。供已安装判断与游戏安装目录解析共用。"""

    def _read():
        steam_root = None
        for hkey, sub, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hkey, sub) as k:
                    steam_root = winreg.QueryValueEx(k, val)[0]
                    break
            except OSError:
                continue
        if not steam_root:
            return []
        dirs = []
        main = Path(steam_root) / "steamapps"
        if main.is_dir():
            dirs.append(main)
        # 多库：libraryfolders.vdf 里的 "path" 字段
        lf = main / "libraryfolders.vdf"
        if lf.is_file():
            try:
                text = lf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                    p = Path(m.group(1)) / "steamapps"
                    if p.is_dir() and p not in dirs:
                        dirs.append(p)
            except OSError:
                pass
        return dirs

    return _cached(("libdirs",), _read)


def _steam_installed_appids():
    """返回 (Steam是否可判断, 已安装 appid 集合)。解析注册表找 Steam 根目录，
    含 libraryfolders.vdf 多库清单。Steam 未安装 → (False, set())，
    调用方此时不得判定任何游戏"已卸载"。"""
    dirs = _steam_library_dirs()
    if not dirs:
        return False, set()
    ids = set()
    for d in dirs:
        try:
            for f in d.glob("appmanifest_*.acf"):
                m = re.match(r"appmanifest_(\d+)\.acf", f.name)
                if m:
                    ids.add(m.group(1))
        except OSError:
            continue
    return True, ids


def steam_installed_games() -> list:
    """读取所有库的 appmanifest_*.acf，返回已安装 Steam 游戏清单：
    [{"appid", "name", "installdir", "state_flags"}]（同 appid 多库取首个，
    与 Steam 实际安装库一致——卸载会删清单，双库残留极罕见）。
    Steam 未安装/读不到返回空列表。纯本地文件读取，无网络。

    说明：StateFlags 不做过滤（Steam 对更新中/云同步中的游戏会给出
    各式各样的标志位，按位过滤会漏游戏），只原样返回供调用方记日志。"""
    games = {}
    for lib in _steam_library_dirs():
        try:
            for f in sorted(lib.glob("appmanifest_*.acf")):
                m = re.match(r"appmanifest_(\d+)\.acf", f.name)
                if not m:
                    continue
                appid = m.group(1)
                if appid in games:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                # "name" 键在 appmanifest 顶层第一个出现，按文本首个匹配取；
                # 值可能含转义（\" 与 \\），先去转义
                nm = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', text)
                if not nm or not nm.group(1).strip():
                    continue      # 清单损坏或正在写入（无名字可显示）
                im = re.search(r'"installdir"\s+"([^"]+)"', text)
                sm = re.search(r'"StateFlags"\s+"(\d+)"', text)
                name = nm.group(1).replace('\\"', '"').replace("\\\\", "\\")
                installdir = im.group(1) if im else ""
                # 与 _steam_install_dir 同一道防线：installdir 带穿越载荷
                # 或绝对路径时丢弃该字段（ACF 属信任边界外数据）
                if installdir and (".." in installdir
                                   or re.match(r"^[A-Za-z]:", installdir)
                                   or installdir.startswith(("\\", "/"))):
                    installdir = ""
                games[appid] = {
                    "appid": appid,
                    "name": name,
                    "installdir": installdir,
                    "state_flags": int(sm.group(1)) if sm else 0,
                }
        except OSError:
            continue
    return list(games.values())


def _steam_install_dir(appid) -> Path | None:
    """Steam 游戏安装目录：appmanifest_<appid>.acf 的 installdir
    → steamapps/common/<installdir>。查不到返回 None（结果带 5 分钟缓存）。"""

    def _read():
        for lib in _steam_library_dirs():
            acf = lib / f"appmanifest_{appid}.acf"
            if not acf.is_file():
                continue
            try:
                text = acf.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'"installdir"\s+"([^"]+)"', text)
            except OSError:
                continue
            if m:
                d = m.group(1)
                # ACF 属信任边界外数据：installdir 带穿越载荷/绝对路径时
                # 不采用，否则"允许的安装根目录"会被指向系统目录
                if ".." in d or re.match(r"^[A-Za-z]:", d) \
                        or d.startswith(("\\", "/")):
                    continue
                return lib / "common" / d
        return None

    # 未找到（None）只缓存 30 秒：新装游戏的 appmanifest 随时会出现，
    # 负缓存 5 分钟会让轮询期自动关联静默失效（2026-09-02 审查 P2-8）
    return _cached(("steam_dir", appid), _read, negative_ttl=30)


def _pick_acf_launch_executable(text) -> str | None:
    """从 ACF 文本挑主程序 exe 名：优先 launch 块里 type="default" 的项，
    否则退化为首个 .exe。

    Steam 清单的 launch 块可能有多项，索引 0 常是重分发/配置检查器
    （type="none"）——全文取首个 executable 会让「启动」跑起 helper 而不是
    游戏（2026-09-10 深度审查 P2-3，构造 ACF 实测复现）。"""
    matches = list(re.finditer(r'"executable"\s+"([^"]+)"', text))
    first_exe = None
    for i, m in enumerate(matches):
        name = m.group(1)
        if not name.lower().endswith(".exe"):
            continue
        if first_exe is None:
            first_exe = name
        # 在该 executable 与下一个 executable 之间的片段里找 type，
        # 避免误取其他区块的 type（限定在条目范围内）
        seg_end = matches[i + 1].start() if i + 1 < len(matches) \
            else min(len(text), m.end() + 400)
        tm = re.search(r'"type"\s+"([^"]+)"', text[m.end():seg_end])
        if tm and tm.group(1).strip().lower() == "default":
            return name
    return first_exe


def _steam_launch_executable(appid) -> Path | None:
    """从 appmanifest_<appid>.acf 的 launch.executable 字段解析主程序路径。

    acf 里的典型格式：
        "launch"
        {
            "0" { "executable" "Game.exe" ... }
        }
    如果解析到且文件真实存在，返回 Path；否则返回 None。
    （结果带 30 秒负缓存；installdir/exe 做越界校验——ACF 属信任边界外，
    与 _steam_install_dir 同一道防线）"""

    def _read():
        for lib in _steam_library_dirs():
            acf = lib / f"appmanifest_{appid}.acf"
            if not acf.is_file():
                continue
            try:
                text = acf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = _pick_acf_launch_executable(text)
            if not m:
                continue
            exe_name = m.replace("\\\\", "\\").replace("/", "\\")
            if not exe_name.lower().endswith(".exe"):
                continue
            # exe 名同样防越界：pathlib 右侧为盘符/绝对路径时整体替换左侧
            if ".." in exe_name or re.match(r"^[A-Za-z]:", exe_name) \
                    or exe_name.startswith(("\\", "/")):
                continue
            # 主程序相对于 installdir，需要先拿到 installdir
            im = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not im:
                continue
            install_dir = im.group(1).replace("\\\\", "\\")
            if ".." in install_dir or re.match(r"^[A-Za-z]:", install_dir) \
                    or install_dir.startswith(("\\", "/")) \
                    or ".." in exe_name:
                continue      # 穿越载荷/绝对路径：不采用
            candidate = lib / "common" / install_dir / exe_name
            if candidate.is_file():
                return candidate
        return None

    return _cached(("steam_launch", appid), _read, negative_ttl=30)


# ---------- Epic ----------

def _epic_manifests_root() -> Path:
    """Epic 清单文件所在目录（*.item）。"""
    return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) \
        / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"


def _epic_manifest(app_name) -> dict | None:
    """读取 Epic *.item 清单，返回整个 JSON dict；找不到或损坏返回 None。
    结果带 5 分钟 TTL 缓存——调用方只读，不得修改返回的 dict。"""

    def _read():
        root = _epic_manifests_root()
        if not root.is_dir():
            return None
        for item in root.glob("*.item"):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("AppName") == app_name:
                return data
        return None

    return _cached(("epic_manifest", app_name), _read, negative_ttl=30)


def _epic_install_dir(app_name) -> Path | None:
    """Epic 游戏安装目录：ProgramData 下 Epic 清单（*.item，JSON）的
    InstallLocation。查不到返回 None。"""
    data = _epic_manifest(app_name)
    if data and data.get("InstallLocation"):
        return Path(data["InstallLocation"])
    return None


def epic_display_name(app_name) -> str | None:
    """从 Epic 清单读取 DisplayName（官方英文名），用于按名称搜索官方封面。
    查不到返回 None。"""
    data = _epic_manifest(app_name)
    if data:
        dn = data.get("DisplayName")
        if dn:
            return str(dn)
    return None


def _epic_launch_executable(app_name) -> Path | None:
    """从 Epic 清单读取 LaunchExecutable，并在 InstallLocation 下定位文件。
    清单属信任边界外数据：InstallLocation/LaunchExecutable 带穿越载荷或
    绝对路径时不采用（与 Steam 侧 _steam_install_dir 同一道防线）。"""
    data = _epic_manifest(app_name)
    if not data or not data.get("InstallLocation"):
        return None
    launch = data.get("LaunchExecutable") or data.get("Executable")
    if not launch:
        return None
    install_loc = str(data["InstallLocation"])
    launch = str(launch).replace("/", "\\")
    if ".." in install_loc or ".." in launch \
            or re.match(r"^[A-Za-z]:", launch) or launch.startswith("\\"):
        return None
    candidate = Path(install_loc) / launch
    if candidate.is_file():
        return candidate
    return None


# ---------- 通用：在游戏目录里找"最像主程序"的 exe ----------

# 这些目录/文件名肯定不是游戏本体（运行时、 redistributable、更新器、叠层等），
# 直接排除，避免把 20MB 的 vcredist 当成主程序。
_REDIST_DIRS = {
    "redist", "_commonredist", "commonredist",
    "directx", "vcredist", "installer", "installers",
    "setup", "support", "crashpad", "cef",
}
_EXE_BLOCKLIST = {
    # 平台/叠层
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe",
    "gameoverlayui64.exe", "vulkandriverquery64.exe",
    "eosbootstrapper.exe", "epiconlineservicesuserhelper.exe",
    "eosoverlayrenderer-win64-shipping.exe",
    # 崩溃/报告/更新
    "crashpad_handler.exe", "crashreporter.exe", "unrealcefsubprocess.exe",
    "node.exe", "updater.exe",
    # Windows / 厂商常驻
    "legionzone.exe",
    "gamebar.exe", "gamebarftserver.exe", "gamebarpresencewriter.exe",
    "gamingservices.exe", "gamingservicesnet.exe",
    # 安装/运行库
    "vcredist_x64.exe", "vcredist_x86.exe", "vcredist.exe",
    "dxsetup.exe", "dxwebsetup.exe",
    "setup.exe", "install.exe", "unins000.exe", "unins001.exe",
}
# 前缀黑名单：带版本号/变体的进程名无法精确列举——目录顶层放崩溃处理器、
# 主程序在子目录时会选错（2026-09-10 深度审查 P3-6）
_EXE_BLOCKLIST_PREFIX = (
    "crashsender", "unitycrashhandler", "qtwebengineprocess",
    "crashreportclient", "crashhandler", "bugreport", "werfault",
)


def _norm_name(s: str) -> str:
    """名称归一化：小写 + 只保留字母数字，方便做"包含"匹配。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_representative_exe(dir_path: Path, game_name: str = "") -> Path | None:
    """在目录里找"最像主程序"的 exe。

    策略：
    1. 排除已知的 redist/安装目录和黑名单进程；
    2. 名字与游戏名相近（互相包含）的加分；
    3. 文件越大越可能是主程序；
    4. 层级越浅越可能是主程序。
    返回得分最高的 exe 路径，找不到返回 None。
    """
    root = Path(dir_path)
    if not root.is_dir():
        return None
    norm_game = _norm_name(game_name)
    candidates = []
    examined = 0
    try:
        for p in root.rglob("*.exe"):
            examined += 1
            if examined > 5000:
                # 巨型目录（100GB 游戏盘）启发式搜索有界：找到的已足够打分
                break
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            rel_parts = p.relative_to(root).parts
            # 排除 redist 等子目录（最后一段是文件名，只检查目录）
            if any(part.lower() in _REDIST_DIRS for part in rel_parts[:-1]):
                continue
            name_lower = p.name.lower()
            if name_lower in _EXE_BLOCKLIST \
                    or name_lower.startswith(_EXE_BLOCKLIST_PREFIX):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            depth = max(0, len(rel_parts) - 1)
            score = 0.0
            # 名字匹配：相互包含就给较高分
            norm_exe = _norm_name(p.stem)
            if norm_game and norm_exe:
                if norm_game in norm_exe or norm_exe in norm_game:
                    score += 80
            # 越浅越好（顶层 exe 通常是启动器/主程序）
            score -= depth * 6
            # 越大越好，按 MB 计（大游戏主程序通常几十 MB）
            score += size / (1024 * 1024)
            candidates.append((score, size, p))
    except OSError:
        pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


# ---------- 统一入口 ----------

def resolve_steam_exe(appid, game_name: str = "") -> str | None:
    """把 Steam AppID 解析为可执行文件路径（优先 manifest 里写的 launch 程序，
    其次在安装目录里启发式搜索）。未安装/找不到返回 None。"""
    exe = _steam_launch_executable(appid)
    if exe:
        return str(exe)
    install = _steam_install_dir(appid)
    if install:
        exe = find_representative_exe(install, game_name)
        if exe:
            return str(exe)
    return None


def epic_app_name_from_launch(value) -> str | None:
    """从 Epic 启动协议 URL 提取 AppName（对应清单 *.item 的 AppName 键）。

    启动器写入的 value 是 URL 编码的复合串：
      com.epicgames.launcher://apps/<CatalogItemId>%3A<Namespace>%3A<AppName>?action=launch
    必须先 unquote 再按冒号取**末段**；旧的 `([^:%?]+)` 正则在 %3A 编码下
    会抓到整个复合串，清单精确匹配永远失败。兼容未编码的裸冒号写法。
    """
    s = str(value or "")
    m = re.search(r"com\.epicgames\.launcher://apps/([^?]+)", s)
    if not m:
        return None
    from urllib.parse import unquote
    app_name = unquote(m.group(1)).split(":")[-1].strip()
    return app_name or None


def resolve_epic_exe(app_name, game_name: str = "") -> str | None:
    """把 Epic AppName 解析为可执行文件路径（优先清单 LaunchExecutable，
    其次在安装目录里启发式搜索）。未安装/找不到返回 None。"""
    exe = _epic_launch_executable(app_name)
    if exe:
        return str(exe)
    install = _epic_install_dir(app_name)
    if install:
        exe = find_representative_exe(install, game_name)
        if exe:
            return str(exe)
    return None


def resolve_game_exe(game: dict) -> str | None:
    """根据游戏记录的 launch 字段解析 exe 路径。

    - 本地 file：直接取 launch.value；
    - Steam：用 steam_id / launch.value 当 AppID；
    - Epic：从协议 URL 提取 AppName。
    找不到返回 None。
    """
    launch = game.get("launch") or {}
    t = launch.get("type")
    if t == "file" and launch.get("value"):
        v = str(launch["value"])
        if Path(v).is_file():
            return v
    if t == "steam":
        sid = str(game.get("steam_id") or launch.get("value") or "")
        if sid:
            return resolve_steam_exe(sid, game.get("name", ""))
    if t == "epic" and launch.get("value"):
        app_name = epic_app_name_from_launch(launch["value"])
        if app_name:
            return resolve_epic_exe(app_name, game.get("name", ""))
    return None


def game_install_roots(game: dict) -> list:
    """游戏进程允许的安装根目录（自动关联只接受这些目录内的进程）：
    - 本地 file：启动 exe 所在目录；
    - Steam：appmanifest 的 installdir 对应目录；
    - Epic：Epic 清单 InstallLocation（尽力而为）。
    这是防止"启动游戏瞬间把后台常驻软件误记成游戏进程"的根本手段。
    返回空列表 = 无法判断。空列表的语义由调用方决定：
    - 启动时采样关联（associate_processes）：视为"不做目录限制"；
    - 轮询期自动关联（_assoc_process）：视为"跳过不关联"（更保守，
      避免同名后台进程在目录未知时被误关联）。
    """
    launch = game.get("launch") or {}
    t = launch.get("type")
    if t == "file" and launch.get("value"):
        p = Path(str(launch["value"])).parent
        return [p] if p.exists() else []
    if t == "steam":
        sid = str(game.get("steam_id") or launch.get("value") or "")
        d = _steam_install_dir(sid) if sid else None
        return [d] if d else []
    if t == "epic" and launch.get("value"):
        app_name = epic_app_name_from_launch(launch["value"])
        if app_name:
            d = _epic_install_dir(app_name)
            return [d] if d else []
    return []

"""启动逻辑：Steam 游戏(steam://)、本地文件、修改器(管理员)、打开目录。"""
import os
from pathlib import Path

from .audit import get_logger

_log = get_logger()


def _shell_execute(verb, path, args=None, workdir=None):
    """ShellExecuteW 统一封装：显式声明 argtypes/restype。
    不声明 restype 时返回值按 32 位 int 处理，64 位句柄被截断后可能
    误进 ret <= 32 的失败分支（成功被当成失败）。"""
    import ctypes
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    ret = shell32.ShellExecuteW(None, verb, str(path),
                                str(args) if args else None,
                                str(workdir) if workdir else None, 1)
    # 失败时返回值 <= 32（SE_ERR_*）；c_void_p 下 NULL 变成 0 同样命中
    return ret if ret else 0


def launch_steam_game(steam_id):
    """经 steam:// 协议启动游戏（Steam 客户端会自动拉起）。
    校验 AppID 为纯数字后用系统协议处理器打开，不经过 shell。"""
    steam_id = str(steam_id)
    if not steam_id.isdigit():
        raise ValueError(f"非法 Steam AppID: {steam_id!r}")
    os.startfile(f"steam://rungameid/{steam_id}")
    _log.info("启动 Steam 游戏 appid=%s", steam_id)


def launch_file(path, args=None):
    """启动本地 exe/文件。统一走 ShellExecuteW 独立传参（不经 shell 解析，
    无注入风险），并把工作目录设为文件所在目录——不少程序靠 cwd 找资源。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")
    ret = _shell_execute("open", p, args, p.parent)
    if ret <= 32:                     # ShellExecute 失败返回 <=32 的错误码
        raise OSError(f"启动失败（错误码 {ret}）")
    _log.info("启动文件 %s", p)


def launch_protocol(url):
    """经系统协议打开。仅允许精确前缀白名单：
    - steam://（launch_steam_game 已单独校验纯数字 AppID，这里兜底）
    - com.epicgames.launcher://
    拒绝 com.xxx:// 等任意协议（防恶意协议被注册为启动项）。"""
    u = str(url)
    if not (u.startswith("steam://") or u.startswith("com.epicgames.launcher://")):
        raise ValueError(f"不支持的协议: {url}")
    os.startfile(u)
    _log.info("经协议启动 %s", u[:60])


def open_folder(path):
    """在资源管理器中打开目录；不存在则创建。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    os.startfile(str(p))
    _log.info("打开目录 %s", p)


def _ascii_launch_path(p) -> str:
    """非 ASCII 路径转 Windows 8.3 短路径（部分修改器对中文路径敏感，
    参照 GameCheatsManager 的"安全启动路径"）。已是 ASCII 或转换失败原样返回。"""
    s = str(p)
    if s.isascii():
        return s
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024)
        # n 是含结尾符的所需长度：缓冲不足时 buf.value 是残缺路径，
        # 必须确认 n 未超缓冲才可用
        if 0 < n <= 1024 and buf.value and buf.value.isascii():
            return buf.value
    except Exception:
        pass
    return s


def launch_trainer(exe_path, as_admin=True, working_dir=None):
    """启动修改器。
    as_admin=True 时经 ShellExecuteW 的 runas 动词提权（弹 UAC，属正常）。
    路径含非 ASCII 字符时先转 8.3 短路径（中文路径兼容）。
    全程经系统 API 独立传参，不经过 shell 解析，无命令注入风险。
    返回 (ok, message)。"""
    p = Path(exe_path)
    if not p.is_file():
        return False, f"文件不存在: {p}"
    try:
        if as_admin:
            run = _ascii_launch_path(p)
            wd = _ascii_launch_path(working_dir or p.parent)
            ret = _shell_execute("runas", run, None, wd)
            if ret == 1223:                   # ERROR_CANCELLED：用户在 UAC 弹窗点了「否」
                # 1223 > 32，不特判会被下面当成功（2026-09-10 深度审查 P2-4）
                return False, "已取消（UAC 提权被拒绝）"
            if ret <= 32:                     # ShellExecute 失败返回 <=32 的错误码
                return False, f"提权启动失败（错误码 {ret}）"
        else:
            # 非 admin 同样走 ShellExecuteW 并显式设置工作目录：
            # os.startfile 不设 lpDirectory，新进程 cwd 继承本程序——
            # 靠 cwd 找资源的修改器会"能启动但功能异常"
            ret = _shell_execute("open", p, None, p.parent)
            if ret <= 32:
                return False, f"启动失败（错误码 {ret}）"
        _log.info("启动修改器 %s (admin=%s)", p, as_admin)
        return True, ""
    except Exception as e:
        return False, f"启动失败: {e}"

"""Windows Defender 白名单：主窗口与设置页共用同一实现。

此前两份拷贝行为不一致——设置页那份不校验 returncode，非管理员运行时
PowerShell 以非零码退出仍弹"完成"（静默假成功）。统一在这里校验并返回
(ok, err)，调用方据此给出真实的成功/失败反馈。"""
import subprocess
from pathlib import Path


def defender_add_exclusion(root) -> tuple:
    """把目录加入 Defender 排除项。返回 (ok, err)：
    ok=True 表示已提交（系统策略仍可能最终拦截，界面会提示用户留意）；
    ok=False 时 err 为原因（非管理员 / 被策略拦截 / powershell 异常等）。"""
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        safe_root = str(root).replace("'", "''")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Add-MpPreference -ExclusionPath '{safe_root}'"],
            capture_output=True, timeout=20,   # 主线程同步调用，长超时会卡死设置页
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            # 非管理员/被策略拦截时 PowerShell 以非零码退出，如实上报
            err = (proc.stderr or b"").decode("gbk", "replace").strip()
            return False, (err or f"PowerShell 退出码 {proc.returncode}")
        return True, ""
    except Exception as e:
        return False, str(e)

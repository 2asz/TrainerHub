"""自动扫描本地修改器：关键字识别 exe，流式后台处理，可取消。"""
import os
from pathlib import Path

# 识别关键字（命中路径/文件名即视为修改器候选）
KEYWORDS = ("fling", "trainer", "修改器", "小辛", "风灵月影", "cheat", "xiaoxin",
            "cheatengine", "wemod")
# 快速剪枝的黑名单目录（不进入；含 venv/环境目录，防止误扫）
BLACKLIST_DIRS = {"$recycle.bin", "system volume information", "windows",
                  "program files", "program files (x86)", "appdata",
                  "node_modules", ".venv", ".git", "dist", "build",
                  "lib", "scripts", "include", "share", "doc", "site-packages",
                  "pip", "setuptools", "_internal", "__pycache__"}
# 修改器一般远小于该体积，超大 exe 直接跳过
MAX_FILE_MB = 300


def scan_folder(root, progress_cb=None, cancel=None):
    """生成器：产出候选 exe 绝对路径。
    显式目录栈迭代（不保留多层生成器/迭代器，深层目录内存友好），
    先按扩展名 + 路径关键词过滤，再 stat 判断大小。
    进度信号按数量节流（每 100 个文件上报一次），避免每文件一次回调。
    progress_cb(已扫描数, 已识别数)；cancel 为 threading.Event，置位即停止。"""
    root = Path(root)
    if not root.is_dir():
        return
    scanned = found = 0
    last_report = 0
    stack = [str(root)]

    def report(force=False):
        nonlocal last_report
        if progress_cb is None:
            return
        if force or scanned - last_report >= 100:
            last_report = scanned
            progress_cb(scanned, found)

    while stack:
        if cancel is not None and cancel.is_set():
            return
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    return
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    low = entry.name.lower()
                    if low in BLACKLIST_DIRS or low.startswith("."):
                        continue
                    stack.append(entry.path)
                    continue
                # 文件：扩展名 + 路径关键词双重过滤后才 stat
                low = entry.name.lower()
                if not low.endswith(".exe"):
                    continue
                # 目录与文件名都要转小写匹配（TRAINER_TOOLS/game.exe 也能命中）
                full_low = directory.lower().replace("\\", "/") + "/" + low
                if not any(k in full_low for k in KEYWORDS):
                    continue
                scanned += 1
                report()
                try:
                    if entry.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                found += 1
                yield Path(entry.path)
    report(force=True)

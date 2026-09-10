"""自动扫描本地修改器：关键字识别 exe，流式后台处理，可取消。"""
import os
from pathlib import Path

# 识别关键字（命中路径/文件名即视为修改器候选）
KEYWORDS = ("fling", "trainer", "修改器", "小辛", "风灵月影", "cheat", "xiaoxin",
            "cheatengine", "wemod")
# 快速剪枝的黑名单目录（不进入；含 venv/环境目录，防止误扫）
BLACKLIST_DIRS = {"$recycle.bin", "system volume information", "windows",
                  "program files", "program files (x86)", "programdata",
                  "appdata", "$windows.~bt", "recovery", "windows.old",
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
    seen_dirs = set()    # (dev, ino)：已访问目录（防 junction 指回祖先成环）

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
            # (dev, ino) 防目录环：NTFS junction 在 Python 3.12+ 不再被视为
            # symlink，指回祖先的 junction 会无限循环——用已访问集合兜底
            st = os.stat(directory, follow_symlinks=False)
            if st.st_ino:
                dir_key = (st.st_dev, st.st_ino)
            else:
                # FAT32/exFAT/部分 SMB 重定向器上 st_ino 恒为 0（非唯一
                # 标识，2026-09-09 审查 P2）：启用 (dev,ino) 集合会把整棵
                # 树判成"已访问"静默漏扫，退化为 realpath 防环
                dir_key = ("p", os.path.normcase(os.path.realpath(directory)))
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
        except OSError:
            continue
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
                # 每个文件都计数（进度语义 = 已扫描文件数，与 docstring 一致）
                scanned += 1
                # 文件：扩展名 + 路径关键词双重过滤后才 stat
                low = entry.name.lower()
                if not low.endswith(".exe"):
                    continue
                # 目录与文件名都要转小写匹配（TRAINER_TOOLS/game.exe 也能命中）
                full_low = directory.lower().replace("\\", "/") + "/" + low
                if not any(k in full_low for k in KEYWORDS):
                    continue
                report()
                try:
                    if entry.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                found += 1
                yield Path(entry.path)
    report(force=True)

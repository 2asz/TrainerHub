"""Trainer Hub 入口：单实例锁 + 统一退出收尾。

新手视角：程序从这里启动。main() 一共做了四件事：
1. 单实例锁：防止同时开两个程序互相打架；
2. 统一 UTF-8：保证中文路径/文字不乱码；
3. 创建 QApplication（每个 Qt 程序必须有且只有一个）；
4. 组装 Library（数据）+ MainWindow（界面），show() 显示窗口，
   然后进入事件循环 app.exec()——从这里开始，界面才"活"起来响应鼠标键盘，
   之前的代码都只是在"搭台子"。
"""
import sys
import logging
import ctypes

from PySide6.QtWidgets import QApplication, QMessageBox


def _single_instance_lock():
    """全局互斥体（单实例）。返回 (锁句柄, 是否已有实例在运行)。
    use_last_error=True：GetLastError 的值必须由 ctypes 保存恢复，否则
    CreateMutexW 与取值之间的 Python 字节码可能覆盖它，双实例漏判 →
    两进程并发写同一个 library.json（跨进程无锁）互相覆盖。"""
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mutex = kernel32.CreateMutexW(None, False, "TrainerHub_SingleInstance")
        if not mutex:
            # 句柄创建失败（极少见）：显式留痕，静默无锁继续跑
            logging.getLogger("trainerhub.audit").warning(
                "单实例互斥体创建失败 err=%s", ctypes.get_last_error())
            return mutex, False
        return mutex, ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return None, False


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    mutex, already_running = _single_instance_lock()
    if already_running:
        QApplication(sys.argv)
        QMessageBox.information(None, "Trainer Hub",
                                "程序已在运行中。")
        return

    # 编码安全：源码与运行时统一 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from app.config import DATA_DIR
    from app.library import Library
    from app.ui.main_window import MainWindow

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Trainer Hub")
    app.setOrganizationName("TrainerHub")

    library = Library()
    window = MainWindow(library)
    window.show()

    code = app.exec()

    # 收尾：释放单实例锁
    if mutex:
        try:
            ctypes.windll.kernel32.CloseHandle(mutex)
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())

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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox


def _single_instance_lock():
    """全局互斥体（单实例）。返回锁对象，退出时释放。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "TrainerHub_SingleInstance")
        return mutex, kernel32
    except Exception:
        return None, None


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    mutex, kernel32 = _single_instance_lock()
    if kernel32 and kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
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

    from app.config import config, DATA_DIR
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
            kernel32.CloseHandle(mutex)
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())

"""本地审计日志：按大小轮转（2MB × 5），磁盘不无限增长。"""
import logging
import logging.handlers
import threading

from .config import DATA_DIR

_LOGGER = None
_LOCK = threading.Lock()


def get_logger():
    # 双重检查 + 锁：多个后台线程首次并发打日志时只挂一次 handler，
    # 否则 RotatingFileHandler 会被挂多份、同一条日志重复写多遍
    if _LOGGER is None:
        with _LOCK:
            if _LOGGER is None:
                _init_logger()
    return _LOGGER


def _init_logger():
    global _LOGGER
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            DATA_DIR / "audit.log", maxBytes=2 * 1024 * 1024,
            backupCount=5, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        _LOGGER = logging.getLogger("trainerhub.audit")
        _LOGGER.setLevel(logging.INFO)
        _LOGGER.addHandler(handler)
        _LOGGER.propagate = False
    except Exception:
        # 日志失败不应影响主流程
        _LOGGER = logging.getLogger("trainerhub.audit")
        _LOGGER.addHandler(logging.NullHandler())


def info(msg): get_logger().info(msg)
def warn(msg): get_logger().warning(msg)
def error(msg): get_logger().error(msg)

# logging 标准级别名别名：调用方统一用 warning（与 logging 一致），warn 保留兼容
warning = warn

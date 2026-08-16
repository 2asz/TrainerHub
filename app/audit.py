"""本地审计日志：按大小轮转（2MB × 5），磁盘不无限增长。"""
import logging
import logging.handlers
from pathlib import Path

from .config import DATA_DIR

_LOGGER = None


def get_logger():
    global _LOGGER
    if _LOGGER is None:
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
    return _LOGGER


def info(msg): get_logger().info(msg)
def warn(msg): get_logger().warning(msg)
def error(msg): get_logger().error(msg)

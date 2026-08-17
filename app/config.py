"""应用配置：JSON 存储于 data/config.json，UTF-8，变更即时保存。"""
import json
import sys
import threading
from pathlib import Path

APP_NAME = "Trainer Hub"
APP_VERSION = "1.0.0"

# 项目根目录：兼容 PyInstaller 打包（frozen 时取 exe 所在目录，保证绿色版可整体拷贝）
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
TRAINERS_ROOT_DEFAULT = PROJECT_ROOT / "trainers"
CONFIG_PATH = DATA_DIR / "config.json"

# 修改器来源（同时是 trainers/ 下的根目录名）
# 注：小辛源已下线；「其他」为历史遗留选项已移除
# （历史数据若仍有 source="其他" 的记录，侧边栏会按动态来源兼容显示）
SOURCES = ["风灵月影", "本地"]

DEFAULTS = {
    "trainers_root": str(TRAINERS_ROOT_DEFAULT),
    "naming_language": "zh",          # zh=中文目录名, en=英文/拼音
    "theme": "dark",                  # dark=深色, light=浅色
    "poll_interval_ms": 2000,          # 进程检测轮询间隔
    "auto_start_trainer": False,       # 游戏运行时自动启动对应修改器（默认关）
    "auto_assoc_process": True,        # 启动游戏后自动记录新进程名
    "download_concurrency": 2,
    "cover_cache_limit": 200,          # 内存封面 LRU 上限
    "window": {"w": 1280, "h": 800},
}


class Config:
    """线程安全的配置单例。"""

    _lock = threading.RLock()

    def __init__(self):
        self._data = dict(DEFAULTS)
        self.last_error = None
        self.load()

    def load(self):
        with self._lock:
            if CONFIG_PATH.exists():
                try:
                    user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    if isinstance(user, dict):
                        for k, v in user.items():
                            if k in DEFAULTS:
                                self._data[k] = v
                except Exception:
                    pass  # 配置损坏则回退默认

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        with self._lock:
            if key in DEFAULTS and self._data.get(key) != value:
                self._data[key] = value
                self._save()

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_PATH)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)

    @property
    def trainers_root(self) -> Path:
        return Path(self.get("trainers_root"))


config = Config()

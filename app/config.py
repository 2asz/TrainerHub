"""应用配置：JSON 存储于 data/config.json，UTF-8，变更即时保存。"""
import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path

APP_NAME = "Trainer Hub"
APP_VERSION = "1.4.0"

# 项目根目录：兼容 PyInstaller 打包（frozen 时取 exe 所在目录，保证绿色版可整体拷贝）
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _writable_dir(path: Path) -> bool:
    """目录可写性探针（mkdir + 写删测试文件）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# 数据目录解析：
# 1. TRAINERHUB_DATA_DIR 环境变量优先（自动化测试隔离用）
# 2. exe/源码旁边的 data\（绿色便携）
# 3. 上面不可写（用户把绿色版解压进 Program Files / 只读盘 / 网络盘）→
#    回落 %LOCALAPPDATA%\TrainerHub\。否则保存全程静默失败：日志初始化失败
#    蒸发、配置每次重启重置、游戏库"能加但重启全丢"且无任何诊断信息
#    （2026-09-03 发布前审查 P0）
_default_data = PROJECT_ROOT / "data"
_default_trainers = PROJECT_ROOT / "trainers"
if not os.environ.get("TRAINERHUB_DATA_DIR") \
        and not _writable_dir(_default_data):
    _fb = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "TrainerHub"
    _default_data = _fb / "data"
    _default_trainers = _fb / "trainers"

DATA_DIR = Path(os.environ.get("TRAINERHUB_DATA_DIR")
                or _default_data)
if os.environ.get("TRAINERHUB_DATA_DIR") and not _writable_dir(DATA_DIR):
    # 测试/手设环境变量指向不可写目录：显式留痕（保存会全程失败）
    logging.getLogger("trainerhub.audit").warning(
        "TRAINERHUB_DATA_DIR 指向不可写目录: %s（保存会失败）", DATA_DIR)
TRAINERS_ROOT_DEFAULT = Path(os.environ.get("TRAINERHUB_TRAINERS_ROOT")
                             or _default_trainers)
CONFIG_PATH = DATA_DIR / "config.json"

# 修改器来源（同时是 trainers/ 下的根目录名）
# 注：小辛源已下线（SRC_COLORS 保留"小辛"键兼容历史数据）；
# 「其他」为历史遗留选项已移除
# （历史数据若仍有 source="其他" 的记录，侧边栏会按动态来源兼容显示）
SOURCES = ["风灵月影", "小幸", "本地"]

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
        # deepcopy：DEFAULTS 里的 "window" 是嵌套 dict，浅拷贝会与默认值
        # 共享同一对象，调用方原地改 get("window") 会污染 DEFAULTS
        self._data = copy.deepcopy(DEFAULTS)
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
            # 类型守门（library 有 _normalize_game，config 此前裸读）：
            # 手改/损坏的类型错误值（如 trainers_root: 123）会让
            # Path(...) 抛 TypeError 打穿下载链路——非默认类型的值回退默认
            #（2026-09-13 审查 P3）
            for k, dv in DEFAULTS.items():
                if type(self._data.get(k)) is not type(dv):
                    self._data[k] = dv

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        with self._lock:
            if key not in DEFAULTS:
                # 未知 key 直接丢弃会吞掉新配置项（拼错名/新增项静默失效），
                # 留一条日志方便发现
                logging.getLogger("trainerhub.audit").warning(
                    "config.set 拒绝未知配置项: %s", key)
                return
            if self._data.get(key) != value:
                self._data[key] = value
                self._save()

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            # fsync 后再 replace（与 library.save 同）：掉电时避免"替换成功
            # 但内容仍在页缓存"导致配置回退默认（2026-09-10 深度审查 P3-4；
            # 后果轻——窗口尺寸/主题复位，故失败不阻断）
            try:
                fd = os.open(tmp, os.O_RDWR)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            tmp.replace(CONFIG_PATH)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            # 配置丢失可回退默认值，不算致命，但必须留痕（此前全工程无人读 last_error）
            logging.getLogger("trainerhub.audit").warning("配置保存失败: %s", e)

    @property
    def trainers_root(self) -> Path:
        # 空串（历史配置被清空过）回退默认目录，避免 Path("") 变成当前工作目录
        return Path(self.get("trainers_root") or TRAINERS_ROOT_DEFAULT)


config = Config()

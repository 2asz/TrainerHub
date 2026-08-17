"""主窗口：侧边栏分类 + 虚拟化卡片墙 + 搜索防抖 + 后台任务管理。

视觉：深色游戏库风格（背景 #0f1115，强调青蓝 #3d8bfd，运行中绿）。
内存设计：
- 卡片墙用 QListView + 自定义 delegate（不建控件，滚动/切换分类不产生对象）；
- 封面异步加载（后台线程取字节，主线程转 QPixmap），内存 LRU 有界 + 磁盘缓存；
- 单实例进程检测定时器；所有网络/磁盘任务走受管线程池。

════════ 新手阅读指南 ════════
这是一个 PySide6（Qt for Python）桌面应用，界面全部由代码绘制，没有 .ui 文件。
先认识 4 个核心概念，读代码就不迷路：
1. 信号与槽（Signal/Slot）：Qt 的事件通知机制。按钮被点击会发出 clicked
   信号，connect(...) 把它接到一个函数（槽）上——类似"订阅-回调"。
   本文件 CardView 里定义的 gameBtnClicked、cardClicked 等 Signal 就是自定义信号。
2. Model/View/Delegate（模型/视图/画笔）：数据和界面分离。
   数据在 GameListModel 里，显示交给 QListView（CardView），中间的"画笔"是
   CardDelegate——它把一条游戏记录画成一张卡片。因为只有看得见的卡片才被画，
   滚动一万款游戏也不卡。Qt 用 rowCount()/data() 问模型"有几行、每行显示什么"。
3. QSS（样式表）：像 CSS 一样给控件上色。:hover 是鼠标悬停、:pressed 是按下，
   按钮"按下去有反馈"就来自这里（见本文件 _style_sheet()，以及 dialogs.py）。
4. QThread / 信号回主线程：网络、扫描等慢操作放后台线程，绝不直接碰界面；
   线程用 Signal 把结果"广播"回主线程，由主线程更新 UI（Qt 规定：UI 只能在
   主线程修改，跨线程操作控件会崩溃）。
新手建议按这个顺序读：main.py → MainWindow._build_ui() → _style_sheet()
→ GameListModel → CardDelegate → CardView → 其余槽函数。
"""
import atexit
import difflib
import hashlib
import re
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

# 关闭窗口时未及时退出的后台线程：脱离父对象并保持强引用，
# 避免 QThread 在运行中被销毁导致崩溃（Qt 崩溃点）；进程退出前最后等待一次
_ORPHAN_THREADS = []


def _adopt_orphan_thread(t):
    """接管未退出的后台线程：断开父子归属，线程结束后自动释放并 deleteLater。"""
    t.finished.connect(lambda: _release_orphan_thread(t))
    t.setParent(None)
    _ORPHAN_THREADS.append(t)


def _release_orphan_thread(t):
    if t in _ORPHAN_THREADS:
        _ORPHAN_THREADS.remove(t)
    t.deleteLater()


@atexit.register
def _wait_orphan_threads():
    for t in _ORPHAN_THREADS:
        t.wait(3000)

from PySide6.QtCore import (QAbstractListModel, QEvent, QPoint, QRect, QSize,
                            Qt, QThread, QThreadPool, QRunnable, QTimer,
                            QObject, Signal, Slot)
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QPixmap,
                            QAction)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QListView,
                               QMainWindow, QMenu, QMessageBox, QProgressDialog,
                               QSplitter, QStackedWidget, QStyledItemDelegate,
                               QStyle, QToolBar, QWidget, QVBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QHBoxLayout, QPushButton, QFrame)

from .. import audit
from ..config import config, SOURCES, DATA_DIR, APP_VERSION
from ..theme import (current as T, set_theme as theme_set, theme_name,
                     load_from_config)
from ..library import Library
from ..steam_import import SteamAppInfo
from ..process_watch import ProcessWatch
from ..launcher import (launch_steam_game, launch_file, launch_protocol,
                        launch_trainer, open_folder)
from ..downloader.base import Downloader
from ..downloader.fling import FlingTrainerDownloader
from ..install_info import _steam_installed_appids, game_install_roots
from ..covers import generate_cover_for_game
from .dialogs import (AddGameDialog, EditGameDialog, ManageTrainersDialog,
                      DownloadDialog, ScanResultDialog, SettingsDialog)

CARD_W, CARD_H = 300, 226
# 封面显示尺寸（与 delegate 中 cover_rect 一致），预缩放后 1:1 绘制，避免滚动时每帧缩放
COVER_W, COVER_H = CARD_W - 32, 108
_COVER_LIMIT_MB = 5


def _btn_rects(rect):
    """卡片底部两个操作按钮的几何（delegate 绘制与 view 命中共用，保证一致）。"""
    w = rect.width()
    y = rect.top() + rect.height() - 38
    bw = (w - 22 - 10) // 2
    game_btn = QRect(rect.left() + 11, y, bw, 28)
    trainer_btn = QRect(rect.left() + 11 + bw + 10, y, bw, 28)
    return game_btn, trainer_btn
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------- 配色
# 颜色统一从 app.theme 读取（深色/浅色主题），这里不再写死十六进制。
# SRC_COLORS 是"来源标签"的徽章色，两套主题通用（浅色下仍清晰）。
SRC_COLORS = {"风灵月影": QColor(255, 176, 32),
              "本地": QColor(140, 200, 120),
              "其他": QColor(170, 170, 180),
              "小辛": QColor(86, 168, 255)}   # 兼容历史数据


# ---------------------------------------------------------------- 封面加载
def _read_limited(path, limit):
    """分块限流读取文件：先按 stat 大小跳过超大文件，再至多读 limit 字节。
    避免 read_bytes()[:limit] 先把整个大文件读入内存。"""
    try:
        if path.stat().st_size > limit:
            return b""
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _cover_cache_path(gid) -> Path:
    """封面缓存文件名：sha256(gid) 十六进制，杜绝恶意/异常 gid 的路径穿越。"""
    return DATA_DIR / "covers" / (hashlib.sha256(str(gid).encode("utf-8")).hexdigest()[:16] + ".png")


# 旧缓存文件名允许的字符集（历史命名如 steam-123、file-abc、game-uuid）；
# 含 / \ : 或 .. 等路径穿越载荷的 gid 一律不参与迁移
_LEGACY_GID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _legacy_cover_path(gid) -> Path | None:
    """旧缓存文件名（{gid}.png）：仅当 gid 为安全字符且不含 '..' 时返回，
    否则返回 None（恶意/异常 gid 不做迁移，杜绝路径穿越）。"""
    s = str(gid)
    if not s or s.startswith(".") or ".." in s \
            or not _LEGACY_GID_RE.match(s):
        return None
    return DATA_DIR / "covers" / f"{s}.png"


class _CoverTask(QRunnable):
    """后台线程：按 磁盘缓存 → 网络官方封面 → 本地封面文件 顺序取图片字节，
    在线程内完成解码/缩放/PNG 落盘（QImage 跨线程安全），
    主线程只做轻量 QPixmap 转换。

    顺序说明：
    - 磁盘缓存是之前下载过的官方封面，优先用；
    - 其次尝试 cover_url 网络官方封面；
    - 最后才用本地离线图标/手动选图做兜底。
    这样离线生成的 exe 图标不会挡住更高清的官方封面。
    """

    def __init__(self, loader, gid, cover_url, cover_file, disk_path):
        super().__init__()
        self.loader = loader
        self.gid = gid
        self.cover_url = cover_url
        self.cover_file = cover_file
        self.disk_path = disk_path

    def run(self):
        from PySide6.QtGui import QImage
        limit = _COVER_LIMIT_MB * 1024 * 1024
        data = b""
        source = ""
        # 1) 已下载过的官方封面缓存
        if self.disk_path:
            data = _read_limited(Path(self.disk_path), limit)
            if data:
                source = "disk"
        # 2) 网络官方封面（高清，优先）
        if not data and self.cover_url:
            from ..security import COVER_IMAGE_HOSTS, safe_get
            data = safe_get(self.cover_url, COVER_IMAGE_HOSTS, timeout=12,
                            max_hops=4, max_bytes=limit,
                            headers={"User-Agent": _UA},
                            label=f"封面下载 gid={self.gid}")
            if data:
                source = "url"
            else:
                audit.warning(f"封面网络下载失败 gid={self.gid} url={self.cover_url[:100]}")
        # 3) 本地离线图标 / 手动选图兜底
        if not data and self.cover_file:
            data = _read_limited(Path(self.cover_file), limit)
            if data:
                source = "file"
            else:
                audit.warning(f"封面本地文件缺失 gid={self.gid} file={self.cover_file}")
        self._finish(data, source)

    def _finish(self, data, source):
        """解码+缩放+落盘；解码失败一律 emit None（主线程统一走损坏/重试分支，
        避免 null QImage 被当作成功导致永久空白且不重试）。

        只有官方来源（缓存/网络）才写回磁盘缓存；本地兜底文件不覆盖缓存，
        避免网络恢复后官方封面被离线图标污染。
        """
        from PySide6.QtGui import QImage
        img = None
        if data:
            img = QImage.fromData(data)
            if img.isNull():
                img = None
            else:
                img = img.scaled(COVER_W, COVER_H,
                                 Qt.KeepAspectRatioByExpanding,
                                 Qt.SmoothTransformation)
                img = img.copy(0, 0, COVER_W, COVER_H)
                if source in ("disk", "url") and self.disk_path:
                    try:
                        img.save(str(self.disk_path), "PNG")
                    except OSError:
                        pass
        self.loader.data_ready.emit(self.gid, img, source)


class CoverLoader(QObject):
    """封面：磁盘缓存 + 内存真 LRU（有界，OrderedDict）。cover_ready 触发重绘。
    取字节/解码/缩放/落盘全部在线程池，GUI 线程只做轻量 QPixmap 转换。
    下载失败的封面进入自动重试队列：网络恢复/加速器开启后无需重启即可补上。"""
    data_ready = Signal(str, object, str)  # gid, QImage/None, 来源(disk/url/file/"")
    cover_ready = Signal(str)

    _RETRY_INTERVAL_MS = 30000     # 重试扫描周期
    _RETRY_COOLDOWN_S = 120        # 同一 gid 两次尝试的最小间隔
    _RETRY_BUDGET = 8              # 每轮最多重试数（防网络差时刷爆请求）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._mem = OrderedDict()     # 真 LRU：move_to_end + popitem(last=False)
        self._inflight = set()
        self._queued = set()          # 已入队（含重试）的 gid，防卡片反复重绘重复入队
        self._retried = set()         # 已因损坏缓存重试过的 gid（防无限循环）
        self._candidates = {}         # gid -> (cover_url, cover_file)，供重试用
        self._queue = deque()         # popleft O(1)，替代 list.pop(0) 的 O(n)
        self._disk = DATA_DIR / "covers"
        try:
            self._disk.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._limit = int(config.get("cover_cache_limit"))
        self._placeholder = self._make_placeholder()
        self.data_ready.connect(self._on_data)
        # 失败封面自动重试
        self._retry_fail_ts = {}      # gid -> 最近失败时间戳
        self._retry_pending = set()   # 已登记待重试的 gid（防重复入队）
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(self._RETRY_INTERVAL_MS)
        self._retry_timer.timeout.connect(self._retry_scan)
        self._retry_timer.start()

    def _make_placeholder(self):
        """无封面占位图：随主题变化的垂直渐变 + "无封面"文字。"""
        t = T()
        pix = QPixmap(COVER_W, COVER_H)
        p = QPainter(pix)
        top, bottom, txt = t["ph_top"], t["ph_bottom"], t["ph_text"]
        for y in range(pix.height()):
            k = y / pix.height()
            r = int(top[0] + (bottom[0] - top[0]) * k)
            g = int(top[1] + (bottom[1] - top[1]) * k)
            b = int(top[2] + (bottom[2] - top[2]) * k)
            p.fillRect(0, y, pix.width(), 1, QColor(r, g, b))
        p.setPen(QColor(*txt))
        p.setFont(QFont("Microsoft YaHei UI", 11))
        p.drawText(pix.rect(), Qt.AlignCenter, "无封面")
        p.end()
        return pix

    def rebuild_placeholder(self):
        """主题切换后重建占位图（旧占位图是上一主题的配色）。"""
        self._placeholder = self._make_placeholder()

    @property
    def placeholder(self):
        return self._placeholder

    def get(self, gid):
        pix = self._mem.get(gid)
        if pix is not None and gid in self._mem:
            self._mem.move_to_end(gid)      # 命中即提升为最近使用
        return pix

    def request(self, gid, cover_url, cover_file):
        # 之前加载失败会在内存里留下 None；如果现在有新的 cover_url/file 来源，
        # 允许重新发起请求，否则删除封面后永远刷不出来。
        if gid in self._mem and self._mem[gid] is None:
            self._mem.pop(gid, None)
        if gid in self._mem or gid in self._inflight or gid in self._queued:
            return
        # 新请求即视为重试成功路径：清掉待重试登记
        self._retry_fail_ts.pop(gid, None)
        self._retry_pending.discard(gid)
        # 缓存文件名基于 sha256(gid)，不受恶意 gid 影响（防路径穿越）
        disk = _cover_cache_path(gid)
        # 兼容旧缓存命名（steam-<id>.png）：迁移到 sha256 名，
        # 避免历史已下载的缓存全部失效导致封面需重新走网络。
        # 仅 gid 为安全字符时迁移（防路径穿越载荷）
        legacy = _legacy_cover_path(gid)
        if legacy is not None and not disk.exists() and legacy.exists():
            try:
                legacy.replace(disk)
            except OSError:
                pass
        # 快路径：磁盘缓存命中（预缩放小图，~0.1ms）直接在主线程加载。
        # 不占线程池并发槽——否则无网时 2 个网络任务占满槽，
        # 命中缓存的封面也要排队几十秒，滚动体验极差
        if disk.exists():
            pix = QPixmap(str(disk))
            if not pix.isNull():
                self._put(gid, pix)
                self.cover_ready.emit(gid)
                return
            disk.unlink(missing_ok=True)
        self._candidates[gid] = (cover_url, cover_file)
        self._queued.add(gid)
        self._queue.append((gid, cover_url, cover_file, disk))
        self._drain()

    def forget(self, gid):
        """封面来源发生变化时（如离线图标生成后、官方 cover_url 查到后），
        清除该 gid 在加载器里的缓存/排队/失败状态，让下一次请求重新加载。"""
        self._mem.pop(gid, None)
        self._queued.discard(gid)
        self._queue = deque(item for item in self._queue if item[0] != gid)
        self._inflight.discard(gid)
        self._retried.discard(gid)
        self._retry_fail_ts.pop(gid, None)
        self._retry_pending.discard(gid)

    def clear_all(self):
        """清空全部封面缓存/排队/失败状态（手动"刷新封面"时用）。
        之后可见卡片重新绘制时会重新发起加载。"""
        self._mem.clear()
        self._queued.clear()
        self._inflight.clear()
        self._retried.clear()
        self._retry_fail_ts.clear()
        self._retry_pending.clear()
        self._candidates.clear()
        self._queue.clear()

    def _drain(self):
        while self._queue and len(self._inflight) < self._pool.maxThreadCount():
            gid, cover_url, cover_file, disk = self._queue.popleft()
            self._queued.discard(gid)
            if gid in self._mem or gid in self._inflight:
                continue
            self._inflight.add(gid)
            self._pool.start(
                _CoverTask(self, gid, cover_url, cover_file, disk))

    @Slot(str, object, str)
    def _on_data(self, gid, img, source):
        # 如果该 gid 已被 forget() 清除（封面来源已变），忽略旧任务的结果，
        # 避免旧任务把 None 写回内存，覆盖新生成的封面。
        if gid not in self._inflight:
            return
        self._inflight.discard(gid)
        self._retry_pending.discard(gid)
        if img is not None:
            # 线程内已解码/缩放，这里只做轻量 QPixmap 转换
            self._put(gid, QPixmap.fromImage(img))
            self.cover_ready.emit(gid)
            # 如果这次用的是本地兜底但游戏其实有官方 cover_url，
            # 仍保留在失败重试队列里，等网络恢复后再换高清封面。
            cover_url, _ = self._candidates.get(gid, ("", ""))
            if source == "file" and cover_url:
                self._retry_fail_ts[gid] = time.time()
            else:
                self._retry_fail_ts.pop(gid, None)
        else:
            # 解码失败：删除损坏磁盘缓存，若未重试过则从 文件/网络 重试一次，
            # 再失败则登记自动重试（网络恢复后无需重启即可补上封面）
            _cover_cache_path(gid).unlink(missing_ok=True)
            if gid not in self._retried:
                self._retried.add(gid)
                cover_url, cover_file = self._candidates.get(gid, ("", ""))
                self._queued.add(gid)
                self._queue.append((gid, cover_url, cover_file, None))
            else:
                self._retried.discard(gid)
                # 保留 candidates：自动重试（网络恢复后）仍需要 cover_url/file 来源
                self._put(gid, None)
                self.cover_ready.emit(gid)
                self._retry_fail_ts[gid] = time.time()
        # 有任务完成即空出一个并发位：继续派发排队中的封面
        self._drain()

    def _retry_scan(self):
        """定时扫描失败封面：超过冷却时间的重新发起加载（网络恢复后自动补上）。"""
        now = time.time()
        budget = self._RETRY_BUDGET
        for gid in list(self._retry_fail_ts):
            if budget <= 0:
                break
            if gid in self._inflight or gid in self._retry_pending:
                continue
            if now - self._retry_fail_ts[gid] < self._RETRY_COOLDOWN_S:
                continue
            budget -= 1
            self._retry_pending.add(gid)   # 防本登记在任务完成前被重复入队
            self._mem.pop(gid, None)        # 移除占位，重新加载
            cover_url, cover_file = self._candidates.get(gid, ("", ""))
            self._queued.add(gid)
            self._queue.append((gid, cover_url, cover_file, _cover_cache_path(gid)))
            self._drain()

    def shutdown(self):
        """关闭窗口时停止重试定时器。"""
        self._retry_timer.stop()

    def _put(self, gid, pix):
        self._mem[gid] = pix
        self._mem.move_to_end(gid)
        while len(self._mem) > self._limit:
            self._mem.popitem(last=False)


# ---------------------------------------------------------------- 数据模型
class GameListModel(QAbstractListModel):
    """卡片墙的数据模型：持有数据 + 过滤（搜索词/分类）+ 运行状态。

    新手视角：这是 Model/View 架构里的 Model（数据层），QListView 是 View（显示层）。
    界面不会直接碰数据，而是通过 rowCount()/data() 让 Qt"问"模型有几行、每行画什么；
    数据一变（reload/dataChanged），界面自动跟着刷新——数据和显示互不纠缠。"""
    Role_Name = Qt.UserRole + 1
    Role_Running = Qt.UserRole + 2
    Role_GameId = Qt.UserRole + 3
    Role_SourceTags = Qt.UserRole + 4
    Role_TrainerCount = Qt.UserRole + 5
    Role_CoverUrl = Qt.UserRole + 6
    Role_CoverFile = Qt.UserRole + 7

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._games = []
        self._running = set()
        self._keyword = ""
        self._source = "全部"
        self._snapshot = []          # 按 revision 缓存的排序快照
        self._snapshot_rev = -1
        self._search_index = {}      # gid -> (小写名, 全拼, 首字母)，随快照重建
        self.reload()

    def _sorted_games(self):
        """按 Library.revision 缓存排序快照：库未变时复用，避免每次 reload 重排序。
        同时缓存搜索索引（小写名/全拼/首字母），拼音转换只做一次。"""
        rev = self._library.revision
        if rev != self._snapshot_rev:
            self._snapshot = self._library.all_games()
            self._search_index = self._build_search_index(self._snapshot)
            self._snapshot_rev = rev
        return self._snapshot

    @staticmethod
    def _build_search_index(games) -> dict:
        """gid -> (小写名, 全拼, 拼音首字母)。拼音失败按空串兜底。"""
        try:
            from pypinyin import lazy_pinyin
        except Exception:
            lazy_pinyin = None
        idx = {}
        for g in games:
            name = g["name"]
            full, initials = "", ""
            if lazy_pinyin and not name.isascii():
                try:
                    parts = lazy_pinyin(name)
                    full = "".join(parts)
                    initials = "".join(p[0] for p in parts if p)
                except Exception:
                    pass
            idx[g["id"]] = (name.casefold(), full, initials)
        return idx

    def _keyword_match(self, kw: str, gid: str) -> bool:
        """关键词匹配：中文名 / 全拼 / 拼音首字母 子串；长词加模糊相似度兜底。"""
        name, full, initials = self._search_index.get(gid, ("", "", ""))
        if kw in name or (full and kw in full) or (initials and kw in initials):
            return True
        if len(kw) >= 4:
            # 模糊兜底：仅对名字本身（容忍拼写错误，如 cyperpank→cyberpunk）。
            # 不对拼音做模糊——拼音子串已覆盖正常输入，模糊会在不同中文游戏
            # 的全拼间产生误报（saierda vs aierdengfahuan 覆盖率可达 0.85）。
            # 归一按较短一方（输入词与名称长度悬殊时 2M/(la+lb) 会压低高覆盖匹配）。
            if name:
                sm = difflib.SequenceMatcher(None, kw, name)
                m_total = sum(b.size for b in sm.get_matching_blocks())
                if m_total >= 4 and m_total / min(len(kw), len(name)) >= 0.75:
                    return True
        return False

    def reload(self):
        self.beginResetModel()
        kw = self._keyword.strip().casefold()
        src = self._source
        out = []
        for g in self._sorted_games():
            if kw and not self._keyword_match(kw, g["id"]):
                continue
            tags = {t["source"] for t in g.get("trainers", [])}
            if src == "无修改器":
                if tags:
                    continue
            elif src == "运行中":
                if g["id"] not in self._running:
                    continue
            elif src != "全部" and src not in tags:
                continue
            out.append(g)
        self._games = out
        self.endResetModel()

    def set_keyword(self, kw):
        kw = kw.strip().casefold()
        if kw != self._keyword:
            self._keyword = kw
            self.reload()

    def set_source(self, src):
        if src != self._source:
            self._source = src
            self.reload()

    def set_running(self, gid, running):
        # 状态先写入全库集合（与当前分类过滤无关），
        # 切回"全部"后 reload() 仍能正确显示运行中徽章
        if (gid in self._running) == running:
            return
        if running:
            self._running.add(gid)
        else:
            self._running.discard(gid)
        if self._source == "运行中":
            self.reload()          # 该分类视图需要增删行
            return
        for i, g in enumerate(self._games):
            if g["id"] == gid:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [self.Role_Running])
                return

    def running_count(self) -> int:
        return len(self._running)

    def cover_updated(self, gid):
        for i, g in enumerate(self._games):
            if g["id"] == gid:
                idx = self.index(i, 0)
                # cover_file 和 cover_url 都可能变化，通知视图两个角色都刷新
                self.dataChanged.emit(idx, idx, [self.Role_CoverUrl,
                                                 self.Role_CoverFile])
                return

    def game_at(self, row):
        return self._games[row] if 0 <= row < len(self._games) else None

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self._games)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._games)):
            return None
        g = self._games[index.row()]
        if role == Qt.DisplayRole or role == self.Role_Name:
            return g["name"]
        if role == self.Role_Running:
            return g["id"] in self._running
        if role == self.Role_GameId:
            return g["id"]
        if role == self.Role_SourceTags:
            return sorted({t["source"] for t in g.get("trainers", [])})
        if role == self.Role_TrainerCount:
            return len(g.get("trainers", []))
        if role == self.Role_CoverUrl:
            return g.get("cover_url") or ""
        if role == self.Role_CoverFile:
            return g.get("cover_file") or ""
        return None


# ---------------------------------------------------------------- 卡片绘制
class CardDelegate(QStyledItemDelegate):
    """性能优化：卡片背景（阴影+圆角+边框）按 4 种状态预渲染为 QPixmap 缓存，
    paint 时一次 drawPixmap；封面预缩放 1:1；每帧仅绘制文字/徽章等轻量元素。

    新手视角：delegate 就是"把一条数据画成什么样"的画笔——数据在模型里，
    这里把每条游戏记录画成一张卡片（封面/名称/来源标签/底部两个按钮），
    具体摆在哪、怎么响应鼠标，由 CardView（QListView）负责。"""

    def __init__(self, covers: CoverLoader, parent=None):
        super().__init__(parent)
        self._covers = covers
        self._name_font = QFont("Microsoft YaHei UI", 9, QFont.DemiBold)
        self._tag_font = QFont("Microsoft YaHei UI", 8)
        self._bg_cache = {}          # (selected, running, hover) -> QPixmap
        self._tag_colors = SRC_COLORS

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    @staticmethod
    def _paint_btn(painter, rect, text, state, normal, hover, pressed, fg):
        """画卡片底部按钮：state 为 "normal"/"hover"/"pressed"，对应三种配色。

        normal/hover/pressed 是 (R, G, B) 元组，fg 是文字颜色。
        按下时按钮整体下沉 1px 并加深底色，产生“真的按下去”的手感。"""
        if state == "pressed":
            bg = pressed
            rect = rect.translated(0, 1)      # 下沉 1px（底色与文字一起下移）
        elif state == "hover":
            bg = hover                        # 悬停：亮一档，提示可点击
        else:
            bg = normal
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*bg))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(*fg))
        painter.drawText(rect, Qt.AlignCenter, text)

    # ---------- 背景预渲染 ----------
    def _bg_pixmap(self, selected, running, hover):
        key = (selected, running, hover)
        pix = self._bg_cache.get(key)
        if pix is None:
            if len(self._bg_cache) > 16:
                self._bg_cache.clear()
            pix = self._render_bg(selected, running, hover)
            self._bg_cache[key] = pix
        return pix

    def _render_bg(self, selected, running, hover):
        t = T()
        w, h = CARD_W - 14, CARD_H - 14
        pix = QPixmap(w, h)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRect(0, 0, w, h)
        # 阴影（一次预渲染，颜色随主题）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*t["shadow"]))
        p.drawRoundedRect(rect.translated(0, 3), 12, 12)
        # 背景 + 边框
        if selected:
            bg, border, bw = QColor(t["card_selected_bg"]), QColor(t["accent"]), 2
        elif running:
            bg, border, bw = QColor(t["card_running_bg"]), QColor(t["running"]), 2
        else:
            bg = QColor(t["card_hover"] if hover else t["card"])
            border = QColor(t["accent"] if hover else t["border"])
            bw = 1
        p.setBrush(bg)
        p.setPen(QPen(border, bw))
        p.drawRoundedRect(rect, 12, 12)
        p.end()
        return pix

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = option.rect.adjusted(7, 7, -7, -7)

        selected = bool(option.state & QStyle.State_Selected)
        running = bool(index.data(GameListModel.Role_Running))
        hover = bool(option.state & QStyle.State_MouseOver)
        gid = index.data(GameListModel.Role_GameId)
        name = index.data(GameListModel.Role_Name) or ""
        tags = index.data(GameListModel.Role_SourceTags) or []
        cnt = index.data(GameListModel.Role_TrainerCount) or 0

        # ---- 背景：一次 drawPixmap（已含阴影/圆角/边框）
        painter.drawPixmap(rect, self._bg_pixmap(selected, running, hover))

        # ---- 封面：1:1 绘制（预缩放），圆角裁剪
        cover_rect = QRect(rect.left() + 9, rect.top() + 9, rect.width() - 18, 108)
        pix = self._covers.get(gid)
        if pix is None:
            pix = self._covers.placeholder
            self._covers.request(gid, index.data(GameListModel.Role_CoverUrl),
                                 index.data(GameListModel.Role_CoverFile))
        path = QPainterPath()
        path.addRoundedRect(cover_rect, 8, 8)
        painter.setClipPath(path)
        painter.drawPixmap(cover_rect, pix)
        painter.setClipping(False)

        # ---- 名称
        t = T()
        info_top = cover_rect.bottom() + 7
        painter.setFont(self._name_font)
        painter.setPen(QColor(t["name_fg"]))
        name_rect = QRect(rect.left() + 11, info_top, rect.width() - 22, 20)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(name, Qt.ElideRight,
                                                          name_rect.width()))

        # ---- 来源标签
        painter.setFont(self._tag_font)
        x = rect.left() + 11
        tag_top = info_top + 19
        for tag in tags[:3]:
            painter.setBrush(self._tag_colors.get(tag, QColor(t["tag_fg"])))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPoint(x + 4, tag_top + 8), 3, 3)
            painter.setPen(QColor(t["tag_fg"]))
            painter.drawText(QRect(x + 11, tag_top, 100, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, tag)
            x += 11 + painter.fontMetrics().horizontalAdvance(tag) + 13

        # ---- 底部双按钮：▶ 启动游戏 / ⚡ 修改器（分离入口）
        game_btn, trainer_btn = _btn_rects(rect)
        btn_font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(btn_font)
        # 从视图读出“悬停/按住的按钮”，命中本卡片时切换按钮画法
        # （hover=高亮、pressed=加深+下沉 1px），这就是点击反馈
        hover_btn = None
        pressed_btn = None
        view = option.widget
        if isinstance(view, CardView):
            hover_btn = view.hover_button()
            pressed_btn = view.pressed_button()

        def btn_state(key):
            if pressed_btn and pressed_btn[0] == gid and pressed_btn[1] == key:
                return "pressed"
            if hover_btn and hover_btn[0] == gid and hover_btn[1] == key:
                return "hover"
            return "normal"

        # 启动游戏（蓝）
        self._paint_btn(painter, game_btn, "▶ 启动游戏", btn_state("game"),
                        t["btn_blue_n"], t["btn_blue_h"], t["btn_blue_p"],
                        (255, 255, 255))
        # 修改器（有→绿，无→灰"添加"）
        if cnt:
            self._paint_btn(painter, trainer_btn, f"⚡ 修改器({cnt})",
                            btn_state("trainer"),
                            t["btn_green_n"], t["btn_green_h"], t["btn_green_p"],
                            (255, 255, 255))
        else:
            self._paint_btn(painter, trainer_btn, "＋ 添加修改器",
                            btn_state("trainer"),
                            t["btn_gray_n"], t["btn_gray_h"], t["btn_gray_p"],
                            t["btn_gray_fg"])

        # ---- 运行中徽章（左上）
        if running:
            chip = QRect(rect.left() + 10, rect.top() + 10, 68, 22)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(*t["running_chip_bg"]))
            painter.drawRoundedRect(chip, 11, 11)
            painter.setPen(QColor(*t["running_chip_fg"]))
            painter.drawEllipse(QRect(chip.left() + 7, chip.top() + 8, 6, 6))
            painter.drawText(QRect(chip.left() + 17, chip.top(), chip.width() - 18,
                                   22), Qt.AlignVCenter, "运行中")

        painter.restore()


class SidebarDelegate(QStyledItemDelegate):
    """侧边栏条目绘制：名称左对齐 + 计数右对齐（替代空格拼接，杜绝参差）。
    选中：圆角背景 + 左侧强调条 + 加粗；悬停：浅背景。"""
    Role_Count = Qt.UserRole + 1

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 34)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = option.rect.adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)

        if selected or hover:
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            t = T()
            painter.fillPath(path, QColor(t["side_selected"] if selected
                                          else t["side_hover"]))
        if selected:
            bar = QRect(rect.left(), rect.top() + 6, 3, rect.height() - 12)
            painter.fillRect(bar, QColor(T()["accent"]))

        name = index.data(Qt.UserRole) or ""
        count = index.data(self.Role_Count)
        name_rect = rect.adjusted(12, 0, -40, 0)
        font = QFont(option.font)
        if selected:
            font.setBold(True)
        painter.setFont(font)
        t = T()
        painter.setPen(QColor(t["text"] if (selected or hover) else t["text_dim"]))
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(
                             name, Qt.ElideRight, name_rect.width()))
        if count is not None:
            painter.setFont(QFont(option.font))
            painter.setPen(QColor(t["side_count_selected"] if selected
                                  else t["side_count"]))
            painter.drawText(rect.adjusted(0, 0, -12, 0),
                             Qt.AlignRight | Qt.AlignVCenter, str(count))
        painter.restore()


class CardView(QListView):
    """卡片墙视图（View 层）：摆放卡片 + 处理鼠标（点卡片/按钮、右键菜单）。

    新手视角：卡片"长什么样"由 CardDelegate 画，"摆在哪、怎么响应鼠标"由这里管；
    按钮的悬停高亮和按下效果状态也在这里维护（_hover_btn/_pressed_btn），
    绘制时 delegate 通过 hover_button()/pressed_button() 查过来。"""
    cardClicked = Signal(str)
    cardDoubleClicked = Signal(str)
    gameBtnClicked = Signal(str)
    trainerBtnClicked = Signal(str)
    rightClicked = Signal(str, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        # 像素级滚动：滚轮/触摸板跟手（默认逐项跳一卡高，很生硬）
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        # 布局批量计算，滚动时减少重排开销
        self.setLayoutMode(QListView.Batched)
        self.setBatchSize(20)
        self.customContextMenuRequested.connect(self._on_context)
        # 卡片按钮反馈状态：(gid, "game"|"trainer") 或 None。
        # 由鼠标事件维护，delegate 绘制时读取，实现悬停高亮与按下效果
        self._hover_btn = None
        self._pressed_btn = None

    # ---------- 卡片按钮反馈：悬停高亮 / 按下效果 ----------
    def _button_at(self, pos):
        """pos（视口坐标）落在哪张卡片的按钮上：返回 (gid, "game"|"trainer") 或 None。"""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        gid = idx.data(GameListModel.Role_GameId)
        game_btn, trainer_btn = _btn_rects(self.visualRect(idx))
        if game_btn.contains(pos):
            return (gid, "game")
        if trainer_btn.contains(pos):
            return (gid, "trainer")
        return None

    def hover_button(self):
        """当前悬停的按钮（供 delegate 绘制时查询）。"""
        return self._hover_btn

    def pressed_button(self):
        """当前按住的按钮（供 delegate 绘制时查询）。"""
        return self._pressed_btn

    def mousePressEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid() and e.button() == Qt.LeftButton:
            gid = idx.data(GameListModel.Role_GameId)
            pos = e.position().toPoint()
            # 按下卡片按钮：只记录“按下”并重绘（立刻出现按压视觉），
            # 不在这里触发动作——等松开且仍在该按钮内才算一次有效点击
            # （标准按钮行为：按住看得到反馈，拖出去松开则取消）
            btn = self._button_at(pos)
            if btn == (gid, "game"):
                self._pressed_btn = btn
                self._hover_btn = btn
                self.viewport().update()
                e.accept()
                return
            if btn == (gid, "trainer"):
                self._pressed_btn = btn
                self._hover_btn = btn
                self.viewport().update()
                e.accept()
                return
            self.cardClicked.emit(gid)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        # 松开时：若之前按住了卡片按钮，判断松开点是否仍在同一按钮内；
        # 是 → 发出对应信号（gameBtnClicked/trainerBtnClicked），否 → 本次点击取消
        if self._pressed_btn is not None and e.button() == Qt.LeftButton:
            gid, which = self._pressed_btn
            cur = self._button_at(e.position().toPoint())
            self._pressed_btn = None
            self._hover_btn = cur
            self.viewport().update()
            if cur == (gid, which):
                if which == "game":
                    self.gameBtnClicked.emit(gid)
                else:
                    self.trainerBtnClicked.emit(gid)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e):
        # 悬停目标变化时才重绘（防止每次移动都触发整屏刷新）
        new = self._button_at(e.position().toPoint())
        if new != self._hover_btn:
            self._hover_btn = new
            self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        # 鼠标离开视图：清空按下/悬停状态，防止按钮“卡”在按下外观
        if self._pressed_btn is not None or self._hover_btn is not None:
            self._pressed_btn = None
            self._hover_btn = None
            self.viewport().update()
        super().leaveEvent(e)

    def mouseDoubleClickEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid():
            self.cardDoubleClicked.emit(idx.data(GameListModel.Role_GameId))
            return
        super().mouseDoubleClickEvent(e)

    def _on_context(self, pos):
        idx = self.indexAt(pos)
        if idx.isValid():
            self.rightClicked.emit(idx.data(GameListModel.Role_GameId),
                                   self.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------- 后台任务
class _SteamImportWorker(QThread):
    """后台线程：解析快捷方式 + 并行拉取 appinfo，逐条发回主线程。

    之前二进制对快捷方式逐个串行调 Steam 官方接口（每个还带超时+重试退避），
    几十个快捷方式非常慢；现在用线程池并行拉取（网络 IO 密集，多线程收益大）。
    found_steam(appid, fallback_name, info)：info 为 None 时 UI 用文件名兜底入库。"""
    found_steam = Signal(str, str, object)
    found_lnk = Signal(str, dict)
    progress = Signal(int, int)
    finished = Signal(int)

    _MAX_WORKERS = 6        # 并行拉取 Steam API 的线程数

    def __init__(self, library, app_info, folder, parent=None):
        super().__init__(parent)
        self._library = library
        self._app_info = app_info
        self._folder = folder
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    @staticmethod
    def _process_file(f, app_info, stop):
        """处理单个快捷方式（线程池 worker）：解析 + 拉 appinfo。
        返回统一 dict；无法识别返回 None。"""
        from pathlib import Path
        from ..steam_import import parse_shortcut
        try:
            parsed = parse_shortcut(f)
        except Exception:
            return None
        if not parsed:
            return None
        stem = Path(f.name).stem
        if "appid" in parsed:
            try:
                info = app_info.fetch(parsed["appid"], cancel=stop)
            except Exception:
                # 单条失败不中止整个导入，按未知处理（UI 用文件名兜底）
                info = None
            return {"kind": "steam", "appid": parsed["appid"],
                    "stem": stem, "info": info}
        if parsed.get("launch"):
            return {"kind": "lnk", "stem": stem, "launch": parsed["launch"]}
        return None

    def run(self):
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor, as_completed
        folder = Path(self._folder)
        files = []
        try:
            if folder.is_dir():
                files = sorted(
                    f for f in folder.iterdir()
                    if f.suffix.lower() in (".url", ".lnk"))
        except OSError:
            pass
        total = len(files)
        done = ok = 0
        ex = ThreadPoolExecutor(max_workers=self._MAX_WORKERS)
        try:
            futs = [ex.submit(self._process_file, f, self._app_info, self._stop)
                    for f in files]
            for fut in as_completed(futs):
                if self._stop.is_set():
                    break
                try:
                    res = fut.result()
                except Exception:
                    res = None
                done += 1
                if res is None:
                    self.progress.emit(done, ok)
                    continue
                if res["kind"] == "steam":
                    info = res["info"]
                    if info and info.get("name"):
                        ok += 1
                    self.found_steam.emit(res["appid"], res["stem"], info)
                else:
                    ok += 1
                    self.found_lnk.emit(res["stem"], res["launch"])
                self.progress.emit(done, ok)
        finally:
            # 取消时提前结束：不再等待未完成的任务（运行中的线程继续跑完即回收）
            ex.shutdown(wait=False, cancel_futures=True)
            # 无论正常结束还是异常/取消，都必须发送 finished，否则进度框永不关闭
            self.finished.emit(total)


class _ScanWorker(QThread):
    """后台扫描文件夹中的疑似修改器。
    仅做纯数据收集，进度/结果/错误经信号发回主线程（UI 只能在主线程操作）。"""
    progress = Signal(int)          # 已识别候选数
    failed = Signal(str)            # 扫描失败原因（权限/磁盘错误等）
    finished = Signal(int)          # 候选总数

    def __init__(self, folder, cancel, parent=None):
        super().__init__(parent)
        self._folder = folder
        self._cancel = cancel
        self._results = []

    @property
    def results(self) -> list:
        return self._results

    def request_cancel(self):
        """关闭窗口等场景：置位取消标记，扫描循环会在下一个文件处停止。"""
        self._cancel.set()

    def run(self):
        from ..scanner import scan_folder
        try:
            for p in scan_folder(self._folder, cancel=self._cancel):
                self._results.append(str(p))
                self.progress.emit(len(self._results))
        except Exception as e:
            # 权限/磁盘等错误不再静默当作"没有结果"，上报给 UI 显示原因
            self.failed.emit(f"{type(e).__name__}: {e}")
        self.finished.emit(len(self._results))


class _CoverFetchWorker(QThread):
    """后台为无封面游戏补拉封面：Steam（按 appid 拉 appdetails）+ Epic（按名搜）。
    命中发 cover_found；确定无结果发 cover_miss；网络失败发 cover_fail
    （主线程据此区分负缓存与网络冷却）。"""
    cover_found = Signal(str, str)     # gid, cover_url
    cover_miss = Signal(str)           # gid（API 正常但确定无结果）
    cover_fail = Signal(str)           # gid（网络失败，下次再试）

    def __init__(self, games, parent=None):
        super().__init__(parent)
        self._games = games            # [(gid, name, kind, steam_id, launch_value)]
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..epic_cover import search_epic_covers
        from ..steam_import import SteamAppInfo
        from ..install_info import epic_display_name
        api = SteamAppInfo()
        try:
            for gid, name, kind, steam_id, launch_value in self._games:
                if self._stop.is_set():
                    break
                try:
                    if kind == "steam" and steam_id:
                        info = api.fetch(str(steam_id), cancel=self._stop)
                        if info and info.get("cover_url"):
                            self.cover_found.emit(gid, info["cover_url"])
                        elif info:
                            self.cover_miss.emit(gid)
                        else:
                            self.cover_fail.emit(gid)
                    elif kind == "epic":
                        # 用多个候选名搜索：库里中文名 + Epic 清单 DisplayName
                        queries = [name]
                        m = re.match(r"com\.epicgames\.launcher://apps/([^:%?]+)",
                                     launch_value)
                        if m:
                            dn = epic_display_name(m.group(1))
                            if dn and dn != name:
                                queries.append(dn)
                        results = search_epic_covers(queries, timeout=12,
                                                     cancel=self._stop)
                        if results:
                            self.cover_found.emit(gid, results[0]["cover_url"])
                        else:
                            self.cover_fail.emit(gid)   # 请求失败/无结果均按可重试
                    else:
                        self.cover_miss.emit(gid)
                except Exception as e:
                    audit.warning(f"官方封面查询异常 {gid} {name}: {e}")
                    self.cover_fail.emit(gid)
        finally:
            api.close()


class _UpdateCheckWorker(QThread):
    """后台检查「官网下载」修改器的最新版本：按游戏名搜官网 → 解析最新版号。
    命中 newer 的发 found（批量一次性上报），完成发 done(检查数, 失败数)。"""
    found = Signal(list)              # [{gid, tid, game, cur, new, entry, page_url}]
    progress = Signal(int, int)       # 已检查, 总数
    done = Signal(int, int)

    def __init__(self, library, adapter, parent=None):
        super().__init__(parent)
        self._library = library
        self._adapter = adapter
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..downloader.fling import FlingTrainerDownloader as F
        tasks = [(gid, t) for gid, t in self._library.all_trainers()
                 if t.get("downloaded")]
        total = len(tasks)
        outdated, fails = [], 0
        for i, (gid, t) in enumerate(tasks):
            if self._stop.is_set():
                break
            game = self._library.get_game(gid)
            if not game:
                continue
            self.progress.emit(i + 1, total)
            try:
                # 中文游戏名先搜全名，搜不到再退回纯 ASCII 名（如
                # "AI LIMIT 无限机兵" → "AI LIMIT"，官网标题是英文）
                results = self._search_results(self._adapter, game["name"])
                page_url = self._best_match_page(game["name"], results)
                if not page_url:
                    fails += 1        # 搜索结果与游戏名对不上，不能盲目更新
                    audit.info(f"更新检查无匹配页: {game['name']}")
                    continue
                entries = self._adapter.resolve_downloads(page_url)
                if not entries:
                    fails += 1
                    audit.info(f"更新检查页面无下载链接: {game['name']} {page_url}")
                    continue
                latest = entries[0]
                if F._version_key(latest.get("version", "")) \
                        > F._version_key(t.get("version", "")):
                    outdated.append({
                        "gid": gid, "tid": t["id"], "game": game["name"],
                        "cur": t.get("version", ""), "new": latest.get("version", ""),
                        "entry": latest, "page_url": page_url,
                    })
            except Exception as e:
                fails += 1
                # 失败必须留痕，否则"显示最新"会掩盖网络/解析问题
                audit.warning(
                    f"更新检查失败 {game['name']}: {type(e).__name__}: {e}")
        if outdated:
            self.found.emit(outdated)
        self.done.emit(total, fails)

    @staticmethod
    def _search_results(adapter, game_name) -> list:
        """官网搜索：先按游戏全名搜；含中文时再用纯 ASCII 名补搜一次
        （官网标题多为英文，中文全名常常搜不到匹配页）。
        结果按页面 URL 去重合并。"""
        queries = [game_name]
        ascii_only = " ".join(re.sub(r"[^\x00-\x7F]+", " ", game_name or "").split())
        if ascii_only and ascii_only != game_name:
            queries.append(ascii_only)
        merged, seen = [], set()
        for q in queries:
            try:
                for r in adapter.search(q):
                    u = r.get("page_url")
                    if u and u not in seen:
                        seen.add(u)
                        merged.append(r)
            except Exception:
                continue      # 单次搜索失败不中断整体检查
        return merged

    @staticmethod
    def _best_match_page(game_name, results) -> str | None:
        """从搜索结果中选与游戏名最匹配的页面（不盲取第一个——
        搜索排序常把同系列其他作品排前面，导致误判"已最新"）。
        归一化（小写去符号）后：互含=强匹配；分数相同时用相似度作为决胜
        （例如 "God of War" vs "God of War Ragnarok" 互含分相同，
          但后者与 God of War 的相似度更低，应选 God of War Trainer 2022）。
        支持中文名：同时用"全名"和"纯 ASCII 部分"参与匹配
        （如 "AI LIMIT 无限机兵" 的 ASCII 部分是 "AI LIMIT"）。
        低于阈值返回 None。"""
        def norm(s):
            return "".join(c for c in (s or "").lower() if c.isalnum())

        variants = [norm(game_name)]
        ascii_part = re.sub(r"[^\x00-\x7F]", "", game_name or "")
        v2 = norm(ascii_part)
        if v2 and v2 not in variants:
            variants.append(v2)

        best_score, best_url, best_ratio = 0.0, None, 0.0
        for r in results or []:
            b = norm(r.get("title", ""))
            if not b:
                continue
            # 该结果在"全名/ASCII 名"里取最高分
            cur_score, cur_ratio = 0.0, 0.0
            for a in variants:
                if not a:
                    continue
                ratio = difflib.SequenceMatcher(None, a, b).ratio()
                if a == b:
                    score = 1.0            # 完全一致（最强）
                elif a in b or b in a:
                    score = 0.9            # 互含（次强）
                else:
                    score = ratio
                if score > cur_score or (score == cur_score and ratio > cur_ratio):
                    cur_score, cur_ratio = score, ratio
            # 分数相同用相似度决胜（防同系列作品抢首位）
            if cur_score > best_score or (cur_score == best_score
                                          and cur_ratio > best_ratio):
                best_score, best_url, best_ratio = cur_score, r.get("page_url"), cur_ratio
        return best_url if best_score >= 0.6 else None


class _UpdateInstallWorker(QThread):
    """按更新清单逐个安装最新版（串行，带进度与取消）。"""
    progress = Signal(int, int, str)          # 已完成, 总数, 游戏名
    one_done = Signal(dict, dict)             # info, item
    all_done = Signal(int, int)               # 成功, 失败

    def __init__(self, library, adapter, items, parent=None):
        super().__init__(parent)
        self._library = library
        self._adapter = adapter
        self._items = items
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        ok = fail = 0
        total = len(self._items)
        for i, item in enumerate(self._items):
            if self._stop.is_set():
                break
            game = self._library.get_game(item["gid"])
            trainer = None
            for t in (game or {}).get("trainers", []):
                if t["id"] == item["tid"]:
                    trainer = t
                    break
            if not game or not trainer:
                fail += 1
                continue
            try:
                dest = self._dest_root(game, trainer)
                info = self._adapter.install(
                    game["name"], item["page_url"], dest,
                    cancel=self._stop, entry=item["entry"])
                self.one_done.emit(info, item)
                ok += 1
            except Exception:
                fail += 1
            self.progress.emit(i + 1, total, game["name"])
        self.all_done.emit(ok, fail)

    def _dest_root(self, game, trainer):
        """更新落点：与下载/手动添加一致——复用已有目录，仅他游戏同名占用时避让。"""
        from .dialogs import trainer_dest_dir
        return trainer_dest_dir(game, self._library, self._adapter.SOURCE)


# 安装目录解析已迁移到 app.install_info，避免 UI 层与封面/进程模块循环引用。
# 这里只保留启动清理本身需要的导入与函数。


def _prune_missing_games(library):
    """启动清理：启动目标已不存在的游戏移除库记录——
    Steam 游戏清单消失（已卸载）/ 本地 exe 被删。
    无法判断的保留（Steam 未安装 / Epic 协议类）。
    与「删除游戏」一致：仅删记录，不动磁盘修改器文件。返回移除的游戏名列表。"""
    import re
    steam_ok, installed = _steam_installed_appids()
    removed = []
    for g in library.all_games():
        launch = g.get("launch") or {}
        t = launch.get("type")
        dead = False
        if t == "steam":
            sid = str(g.get("steam_id") or launch.get("value") or "")
            if steam_ok and sid and sid not in installed:
                dead = True
        elif t == "file":
            v = launch.get("value")
            if v and not Path(v).is_file():
                dead = True
        if dead:
            library.remove_game(g["id"])
            removed.append(g["name"])
    return removed


def _prune_missing_covers(library) -> int:
    """清理"cover_file 指向的文件已被删除"的失效引用。

    用户手动删掉 data/covers/ 后，library.json 里还留着旧路径；
    如果不清掉，封面补拉逻辑会以为"有封面"而跳过，导致永远不重新生成。
    返回清理条数。"""
    n = 0
    for g in library.all_games():
        cf = g.get("cover_file")
        if cf and not Path(cf).is_file():
            library.update_game(g["id"], cover_file=None)
            n += 1
    return n


class _StartupCleanWorker(QThread):
    """启动清理后台线程：磁盘已删修改器 / 已卸载游戏 / 失效封面引用。

    之前这三步在 MainWindow.__init__ 主线程里跑：Steam 清单解析 + 逐个
    is_file() 检查，游戏多时首启会卡住界面。现在丢后台线程跑，
    界面先显示出来，清理完发信号由主线程刷新模型。库操作方法带锁，线程安全。
    """
    done = Signal(int, int, int)      # 移除修改器数, 移除游戏数, 失效封面数

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library

    def run(self):
        try:
            pruned = self._library.prune_missing_trainers()
        except Exception:
            pruned = 0
        try:
            gone = len(_prune_missing_games(self._library))
        except Exception:
            gone = 0
        try:
            stale = _prune_missing_covers(self._library)
        except Exception:
            stale = 0
        self.done.emit(pruned, gone, stale)


class _OfflineCoverWorker(QThread):
    """后台离线封面生成：给没有有效 cover_file 的游戏生成 exe 图标/首字母封面。

    之前在主线程跑：每款游戏提取 exe 图标（~70ms）+ Steam ACF 解析，
    几十款游戏首启就卡死。现在在后台线程生成（covers.py 已改纯 QImage，
    线程安全），主线程只收 (gid, 封面路径) 结果更新库与卡片。"""
    cover_done = Signal(str, str)     # gid, 封面路径（失败为空串）
    batch_done = Signal()

    def __init__(self, games, parent=None):
        super().__init__(parent)
        self._games = games            # 游戏字典快照
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..covers import generate_cover_for_game
        for g in self._games:
            if self._stop.is_set():
                break
            try:
                cover = generate_cover_for_game(g)
            except Exception as e:
                audit.warning(f"离线封面生成失败 {g.get('id')} {g.get('name')}: {e}")
                cover = None
            self.cover_done.emit(g.get("id", ""), cover or "")
        self.batch_done.emit()


class MainWindow(QMainWindow):
    def __init__(self, library: Library):
        super().__init__()
        self._library = library
        # 启动清理（后台线程）：磁盘已删修改器 / 已卸载游戏 / 失效封面引用。
        # 之前在主线程同步执行，Steam 清单解析 + 文件检查会让首启卡顿；
        # 现在界面先显示，清理完由信号回主线程刷新模型。
        self._startup_clean = _StartupCleanWorker(library, self)
        self._startup_clean.done.connect(self._on_startup_clean)
        self._startup_clean.start()
        self._app_info = SteamAppInfo()
        self._downloader = Downloader()
        self._fling = FlingTrainerDownloader(self._downloader)
        self._covers = CoverLoader(self)
        self._model = GameListModel(library, self)
        self._delegate = CardDelegate(self._covers, self)
        self._proc_watch = ProcessWatch(library, self)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(int(config.get("download_concurrency")) + 1)
        self._import_dlg = None
        self._import_task = None
        self._import_was_auto = False
        self._current_cat = "全部"

        # 搜索防抖
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(
            lambda: self._model.set_keyword(self._search_box.text()))

        # 库保存防抖
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2000)
        self._save_timer.timeout.connect(self._do_save)

        # 启动清理：移除 process_names 里被误关联的后台常驻进程
        # （node/updater/EOS/Steam 叠层等，名单见 process_watch._NON_GAME_EXES）。
        # 这些进程常驻后台，不清掉会让游戏"已退出却仍显示运行中"
        n = self._proc_watch.prune_background_process_names()
        if n:
            audit.info(f"启动清理：移除 {n} 个误关联的后台进程名（防运行中误判）")
            self._mark_save()

        # 按配置恢复主题（深色/浅色），再构建 UI（样式表用当前主题生成）
        load_from_config()

        self._build_ui()
        self._connect()
        self._restore_state()
        self._proc_watch.start()
        self._save_timer.start()
        self._maybe_auto_import()
        # 自动启动修改器（设置开启时）：游戏进程出现即启动对应修改器
        self._proc_watch.about_to_change.connect(self._auto_launch_trainer)
        # 启动 2 秒后为无封面的 Steam/Epic 游戏补拉官方封面（懒加载）
        QTimer.singleShot(2000, self._schedule_cover_fetch)
        self._start_cover_retry_timer()   # 周期重试：网络恢复后自动补上

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self):
        # 窗口标题带上应用版本号（发布后用户一眼可辨识版本，反馈问题好对齐）
        self.setWindowTitle(f"Trainer Hub v{APP_VERSION} · 修改器整合")
        self.resize(config.get("window").get("w", 1280),
                    config.get("window").get("h", 800))
        self.setMinimumSize(980, 620)   # 卡片墙至少 2 列 + 侧边栏
        self._build_toolbar()
        self._build_sidebar()

        # 卡片视图 + 空状态（QStackedWidget 切换）
        self._view = CardView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self._view.setStyleSheet("QListView { background: transparent; border: none; }")

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)
        self._empty_page = self._build_empty_page()
        self._stack.addWidget(self._empty_page)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(1)
        self.setCentralWidget(splitter)

        self.setStyleSheet(self._style_sheet())
        self._sync_ui_state()

    def _build_toolbar(self):
        # 新手视角：窗口顶部那排按钮。QToolBar 是容器，addAction 把"操作"
        # （QAction）加进去；每个操作绑定一个函数（槽），点击即触发——
        # 这是信号/槽机制的基本用法（操作对象发出 triggered 信号）
        tb = QToolBar("工具栏", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setObjectName("mainToolbar")
        self.addToolBar(tb)

        self._brand_label = QLabel("🎮  Trainer Hub", self)
        self._brand_label.setStyleSheet(
            "color: %s; font-size: 15px; font-weight: bold;"
            "padding: 0 14px 0 6px;" % T()["text"])
        tb.addWidget(self._brand_label)

        def act(text, slot):
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        self._act_add = act("➕ 添加游戏", self.add_game)
        self._act_steam = act("📥 导入 Steam", self.import_steam)
        self._act_scan = act("🔧 扫描修改器", self.scan_trainers)
        self._act_dl = act("⬇️ 下载修改器", self.download_trainer)
        self._act_upd = act("🔄 检查更新", self.check_trainer_updates)
        # 手动刷新（等效重启）：清理误关联进程 + 立即重判运行状态
        self._act_refresh = act("🔁 刷新", self.refresh_state)
        self._act_refresh.setToolTip(
            "立即重新检测游戏运行状态，并清理误关联的后台进程（无需重启）")
        # 手动刷新封面：一次性重试所有失败/缺失封面（官方封面需要网络就绪）
        self._act_cover = act("🖼 刷新封面", self.refresh_covers)
        self._act_cover.setToolTip(
            "重新生成离线封面，并立刻重试所有未拉到的官方封面（开加速器后点这个）")
        tb.addSeparator()
        self._act_white = act("🛡️ 白名单", self.defender_whitelist)
        self._act_white.setToolTip("Windows Defender 白名单（避免修改器被误报拦截）")
        self._act_set = act("⚙️ 设置", self.open_settings)

        # 弹性占位：把搜索框推到最右，窄窗口时按钮优先保留空间
        from PySide6.QtWidgets import QSizePolicy
        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self._search_box = QLineEdit(self)
        self._search_box.setPlaceholderText("🔎  搜索游戏…（Ctrl+F）")
        self._search_box.setMinimumWidth(190)
        self._search_box.setMaximumWidth(320)
        self._search_box.setClearButtonEnabled(True)
        self._search_box.setObjectName("searchBox")
        tb.addWidget(self._search_box)

        # 统计信息移到底部状态栏：工具栏只留操作，信息归状态栏（分层更清晰）
        self._stats_label = QLabel("", self)
        self._stats_label.setStyleSheet(
            "color: %s; padding: 0 12px;" % T()["text_dim"])
        self.statusBar().addWidget(self._stats_label)

    def _build_sidebar(self):
        # 新手视角：左侧分类列表。QListWidget 是"开箱即用的列表控件"
        # （不像卡片墙要自定义 delegate）；currentRowChanged 信号在
        # 选中行变化时通知主窗口切分类（_on_cat_changed）
        self._sidebar = QListWidget(self)
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setFixedWidth(176)
        self._sidebar.setSpacing(2)
        self._sidebar.setItemDelegate(SidebarDelegate(self._sidebar))
        self._cat_keys = []
        self._cat_items = {}
        for key in self._cat_list():
            it = QListWidgetItem()
            it.setText(key)                 # 无障碍/辅助技术仍可读到名称
            it.setData(Qt.UserRole, key)
            self._sidebar.addItem(it)
            self._cat_keys.append(key)
            self._cat_items[key] = it
        self._sidebar.setCurrentRow(0)
        self._sidebar.currentRowChanged.connect(self._on_cat_changed)

    def _cat_list(self):
        cats = ["全部", "运行中"]
        for s in SOURCES:
            cats.append(s)
        for s in sorted(self._library.game_source_set()):   # 兼容历史来源
            if s not in cats:
                cats.append(s)
        cats.append("无修改器")
        return cats

    def _build_empty_page(self):
        page = QWidget(self)
        page.setObjectName("emptyPage")
        lay = QVBoxLayout(page)
        lay.addStretch(2)
        icon = QLabel("🎮", page)
        icon.setStyleSheet("font-size: 52px;")
        icon.setAlignment(Qt.AlignCenter)
        self._empty_title = QLabel("", page)
        self._empty_title.setStyleSheet(
            "color: %s; font-size: 20px; font-weight: bold;" % T()["text"])
        self._empty_title.setAlignment(Qt.AlignCenter)
        self._empty_desc = QLabel("", page)
        self._empty_desc.setStyleSheet("color: %s; font-size: 13px;" % T()["text_dim"])
        self._empty_desc.setAlignment(Qt.AlignCenter)
        btn_row = QWidget(page)
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 18, 0, 0)
        br.setAlignment(Qt.AlignCenter)
        self._empty_btn_import = QPushButton("📥 导入 Steam 游戏", page)
        self._empty_btn_import.setObjectName("emptyBtn")
        self._empty_btn_import.clicked.connect(self.import_steam)
        self._empty_btn_add = QPushButton("➕ 手动添加游戏", page)
        self._empty_btn_add.setObjectName("emptyBtn")
        self._empty_btn_add.clicked.connect(self.add_game)
        br.addWidget(self._empty_btn_import)
        br.addWidget(self._empty_btn_add)
        lay.addWidget(icon)
        lay.addSpacing(6)
        lay.addWidget(self._empty_title)
        lay.addSpacing(4)
        lay.addWidget(self._empty_desc)
        lay.addWidget(btn_row)
        lay.addStretch(3)
        return page

    def _style_sheet(self):
        t = T()
        return f"""
        QMainWindow, QWidget {{ background: {t['bg']}; color: {t['text']};
                               font-size: 13px; font-family: "Microsoft YaHei UI"; }}
        QToolBar#mainToolbar {{ background: {t['toolbar']}; border: none; padding: 5px;
                               border-bottom: 1px solid {t['border']}; spacing: 2px; }}
        /* 按钮点击反馈：:hover=悬停高亮，:pressed=按下时颜色加深 +
           内容下沉 1px（增加/减少上边距实现的"按压感"） */
        QToolButton {{ padding: 6px 11px; border-radius: 6px; color: {t['text']}; }}
        QToolButton:hover {{ background: {t['toolbtn_hover']}; }}
        QToolButton:pressed {{ background: {t['accent_dark']};
                              padding: 7px 11px 5px 11px; }}
        QToolBar::separator {{ background: {t['border']}; width: 1px; margin: 4px 8px; }}
        QLineEdit#searchBox {{ background: {t['search_bg']}; border: 1px solid {t['border']};
                              border-radius: 14px; padding: 5px 12px;
                              selection-background-color: {t['accent']}; }}
        QLineEdit#searchBox:focus {{ border: 1px solid {t['accent']}; }}
        QListWidget#sidebar {{ background: {t['sidebar']}; border: none; padding-top: 6px;
                              font-size: 13px; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; }}
        QScrollBar::handle:vertical {{ background: {t['scrollbar']}; border-radius: 5px;
                                      min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: {t['scrollbar_hover']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QMenu {{ background: {t['menu']}; border: 1px solid {t['border']}; padding: 5px;
                border-radius: 6px; }}
        QMenu::item {{ padding: 7px 24px; border-radius: 4px; color: {t['text']}; }}
        QMenu::item:selected {{ background: {t['accent_dark']}; }}
        QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
        QMessageBox {{ background: {t['msgbox']}; }}
        QMessageBox QLabel {{ color: {t['text']}; }}
        QStatusBar {{ background: {t['status']}; border-top: 1px solid {t['border']};
                     color: {t['text_dim']}; font-size: 12px; }}
        QStatusBar::item {{ border: none; }}
        QPushButton#emptyBtn {{
            background: {t['empty_btn']}; border: 1px solid {t['border']};
            border-radius: 8px; padding: 9px 18px; font-size: 13px;
            color: {t['text']};
        }}
        QPushButton#emptyBtn:hover {{ background: {t['empty_btn_hover']};
                                     border-color: {t['accent']}; }}
        QPushButton#emptyBtn:pressed {{ background: {t['empty_btn_pressed']};
                                        border-color: {t['accent_dark']};
                                        padding: 10px 18px 8px 18px; }}
        QWidget#emptyPage {{ background: {t['bg']}; }}
        """

    def _connect(self):
        # 新手视角：这里把"信号"接到"槽"。信号是 Qt 的事件通知（例如
        # 卡片按钮被点击、游戏进程出现），connect 表示"事件一发生就调用这个函数"：
        # 例如双击卡片 → cardDoubleClicked → launch_game（启动游戏）
        self._view.cardDoubleClicked.connect(self.launch_game)
        self._view.gameBtnClicked.connect(self.launch_game)
        self._view.trainerBtnClicked.connect(self._trainer_btn)
        self._view.rightClicked.connect(self._context_menu)
        # 选中态变化由 Qt 自动重绘，无需整表 repaint
        self._search_box.textChanged.connect(lambda _: self._search_timer.start())
        self._proc_watch.running_changed.connect(self._model.set_running)
        # 运行状态变化 → 侧边栏「运行中」计数即时刷新
        self._proc_watch.running_changed.connect(lambda *_: self._refresh_cat_counts())
        self._covers.cover_ready.connect(self._model.cover_updated)
        self._model.modelReset.connect(self._sync_ui_state)
        # 快捷键：Ctrl+F 聚焦搜索；搜索框内 Esc 清空
        from PySide6.QtGui import QShortcut, QKeySequence
        sc = QShortcut(QKeySequence.Find, self)
        sc.activated.connect(self._focus_search)
        self._search_box.installEventFilter(self)

    def _focus_search(self):
        self._search_box.setFocus()
        self._search_box.selectAll()

    def eventFilter(self, obj, e):
        if obj is self._search_box and e.type() == QEvent.KeyPress \
                and e.key() == Qt.Key_Escape:
            if self._search_box.text():
                self._search_box.clear()   # 第一次 Esc 清空搜索
            else:
                self._search_box.clearFocus()
            return True
        return super().eventFilter(obj, e)

    def _on_cat_changed(self, row):
        if 0 <= row < len(self._cat_keys):
            self._current_cat = self._cat_keys[row]
            self._model.set_source(self._current_cat)

    # ------------------------------------------------------------ UI 状态
    def _on_startup_clean(self, pruned, gone, stale):
        """后台启动清理完成：写日志 + 刷新模型（此时窗口已显示，不再卡首启）。"""
        if pruned:
            audit.info(f"启动清理：{pruned} 个修改器文件不存在，已移除对应记录")
        if gone:
            audit.info(f"启动清理：{gone} 款游戏已卸载，移除记录")
        if stale:
            audit.info(f"启动清理：{stale} 条封面引用已失效，等待重新生成")
        if pruned or gone or stale:
            self._model.reload()
            self._mark_save()

    def _sync_ui_state(self):
        """模型变化后刷新：侧边栏计数 / 状态栏统计 / 空状态切换。"""
        games = self._library.all_games()   # 一次快照，供多处复用（避免重复排序）
        self._refresh_cat_counts(games)
        n = len(games)
        m = sum(len(g.get("trainers", [])) for g in games)
        r = self._model.running_count()
        parts = [f"🎮 {n} 款游戏", f"🛠️ {m} 个修改器"]
        if r:
            parts.append(f"▶ {r} 运行中")
        self._stats_label.setText("   ·   ".join(parts))

        empty = self._model.rowCount() == 0
        kw = self._search_box.text().strip()
        no_any = not games
        if empty:
            self._stack.setCurrentWidget(self._empty_page)
            if no_any:
                self._empty_title.setText("欢迎使用 Trainer Hub")
                self._empty_desc.setText("导入桌面游戏，或手动添加，开始管理你的修改器")
                self._empty_btn_import.setVisible(True)
                self._empty_btn_add.setVisible(True)
            else:
                self._empty_title.setText("没有匹配的游戏")
                self._empty_desc.setText("试试调整搜索词或切换分类")
                self._empty_btn_import.setVisible(False)
                self._empty_btn_add.setVisible(False)
        else:
            self._stack.setCurrentWidget(self._view)

    def _refresh_cat_counts(self, games=None):
        if games is None:
            games = self._library.all_games()
        for key, it in self._cat_items.items():
            if key == "全部":
                n = len(games)
            elif key == "运行中":
                n = self._model.running_count()
            elif key == "无修改器":
                n = sum(1 for g in games
                        if not g.get("trainers"))
            else:
                n = sum(1 for g in games
                        if any(t["source"] == key for t in g.get("trainers", [])))
            it.setData(SidebarDelegate.Role_Count, n)

    # ------------------------------------------------------------ 工具函数
    def _game(self, gid):
        return self._library.get_game(gid)

    def _require_game(self, gid) -> bool:
        return self._game(gid) is not None

    def _mark_save(self):
        if not self._save_timer.isActive():
            self._save_timer.start()

    def _do_save(self):
        """保存库；失败时提示用户（不静默丢失数据）。"""
        if not self._library.save():
            err = getattr(self._library, "last_error", None) or "未知错误"
            self._warn_once(f"游戏库保存失败：{err}")

    _warned_save = False
    def _warn_once(self, msg):
        if not MainWindow._warned_save:
            MainWindow._warned_save = True
            QMessageBox.warning(self, "保存失败", msg + "\n（本次会话不再重复提示）")

    # ------------------------------------------------------------ 启动
    def launch_game(self, gid):
        game = self._game(gid)
        if not game:
            return
        launch = game.get("launch") or {}
        try:
            if launch.get("type") == "steam" and game.get("steam_id"):
                before = self._proc_watch.capture_processes()
                launch_steam_game(game["steam_id"])
                self._schedule_proc_assoc(gid, before)
            elif launch.get("type") == "epic" and launch.get("value"):
                before = self._proc_watch.capture_processes()
                launch_protocol(launch["value"])
                self._schedule_proc_assoc(gid, before)
            elif launch.get("type") == "file" and launch.get("value"):
                launch_file(launch["value"], launch.get("args"))
                # auto_assoc_process：记录该 exe 的进程名（受设置控制）
                if config.get("auto_assoc_process"):
                    exe = str(launch["value"]).rsplit("\\", 1)[-1]
                    if exe.lower().endswith(".exe"):
                        pns = list(game.get("process_names") or [])
                        if exe not in pns:
                            pns.append(exe)
                            self._library.update_game(gid, process_names=pns)
                            self._mark_save()
            else:
                QMessageBox.information(
                    self, "无法启动", "该游戏未配置启动方式，请在「编辑」中设置。")
        except (ValueError, OSError) as e:
            # Steam/协议/本地启动失败统一捕获（os.startfile 常见 OSError），不冒泡
            QMessageBox.warning(self, "无法启动", str(e))

    def _schedule_proc_assoc(self, gid, before):
        """Steam/Epic 游戏无初始进程名，靠启动前后进程快照对比自动关联：
        启动后 3s、7s 各采样一次新增 PID（避免晚启动的进程漏捕），
        合并后按 exe 路径/进程名过滤（排除系统与平台辅助进程）再入库。
        仅 auto_assoc_process 开启时生效。"""
        if not config.get("auto_assoc_process"):
            return
        new = []

        def _sample(after=None):
            # 采样"启动后新出现"的进程：(exe, name) 元组，两次采样合并去重
            if after is None:
                after = self._proc_watch.capture_processes()
            for pid in set(after) - set(before):
                item = after[pid]
                if item not in new:
                    new.append(item)

        def _apply():
            # 7s 最后一次采样后，只关联"最后一次采样时仍存活"的进程——
            # 启动瞬间被拉起的临时进程（安装器/更新器/崩溃报告等）往往
            # 几秒内自己退出，误关联它们会让游戏"已退出却仍显示运行中"
            after = self._proc_watch.capture_processes()
            _sample(after)
            alive = set(after.values())
            keep = [item for item in new if item in alive]
            # 只关联游戏安装目录内的进程（Steam installdir / 本地启动 exe 目录 /
            # Epic 清单位置）——后台常驻软件即使同时被拉起也不会被误记成游戏进程
            game = self._game(gid)
            roots = game_install_roots(game) if game else []
            self._proc_watch.associate_processes(gid, keep, roots=roots)
            if self._library.is_dirty():
                self._mark_save()

        QTimer.singleShot(3000, _sample)
        QTimer.singleShot(7000, _apply)

    def launch_trainer(self, gid, tid):
        game = self._game(gid)
        if not game:
            return
        trainer = next((t for t in game.get("trainers", []) if t["id"] == tid), None)
        if not trainer:
            return
        # 平衡型安全策略：官网下载的修改器首次运行需一键确认
        if trainer.get("downloaded") and not trainer.get("first_run_confirmed"):
            ret = QMessageBox.question(
                self, "安全确认",
                f"该修改器由本软件从官网自动下载。\n"
                f"SHA-256: {(trainer.get('sha256') or '未知')[:16]}…\n\n"
                f"首次运行需确认（确认后将直接启动，不再询问）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            self._library.update_trainer(gid, tid, first_run_confirmed=True)
            self._mark_save()
        ok, msg = launch_trainer(trainer["exe_path"], as_admin=True)
        if not ok:
            QMessageBox.warning(self, "启动失败", msg)

    def _auto_launch_trainer(self, gid, running):
        """自动启动修改器：游戏进程出现时（且设置开启）自动运行对应修改器。
        只自动启动「官方下载、已完成首次确认」的修改器（安全策略，避免未经确认的执行）。"""
        if not running or not config.get("auto_start_trainer"):
            return
        game = self._game(gid)
        if not game:
            return
        for t in game.get("trainers", []):
            if t.get("downloaded") and t.get("first_run_confirmed"):
                self.launch_trainer(gid, t["id"])
                break

    def _trainer_btn(self, gid):
        """卡片「⚡ 修改器」按钮：有则启动（多个弹菜单选），无则引导添加。"""
        from PySide6.QtGui import QCursor
        game = self._game(gid)
        if not game:
            return
        trainers = game.get("trainers", [])
        if not trainers:
            menu = QMenu(self)
            a_manage = menu.addAction("🛠 管理修改器…")
            a_dl = menu.addAction("⬇️ 官网下载…")
            act = menu.exec(QCursor.pos())
            if act is a_manage:
                self.open_manage(gid)
            elif act is a_dl:
                dlg = DownloadDialog(self._library, gid, self._fling,
                                     self._downloader, self._pool, self)
                if dlg.exec():
                    self._model.reload()
                    self._mark_save()
            return
        if len(trainers) == 1:
            self.launch_trainer(gid, trainers[0]["id"])
            return
        menu = QMenu(self)
        for t in trainers:
            label = f"{t['source']} · {t['name']}"
            menu.addAction(label, lambda checked=False, tid=t["id"]:
                           self.launch_trainer(gid, tid))
        menu.exec(QCursor.pos())

    def open_manage(self, gid):
        if not self._require_game(gid):
            return
        dlg = ManageTrainersDialog(self._library, gid, self)
        dlg.exec()
        self._model.reload()
        self._mark_save()

    # ------------------------------------------------------------ 右键菜单
    def _context_menu(self, gid, global_pos):
        game = self._game(gid)
        if not game:
            return
        menu = QMenu(self)
        act_launch = menu.addAction("▶ 启动游戏")
        menu.addSeparator()
        sub = menu.addMenu("⚡ 启动修改器")
        trainers = game.get("trainers", [])
        if trainers:
            for t in trainers:
                sub.addAction(f"{t['source']} · {t['name']}")
        else:
            a = sub.addAction("（无修改器）")
            a.setEnabled(False)
        menu.addSeparator()
        act_manage = menu.addAction("🛠 管理修改器…")
        act_edit = menu.addAction("✏️ 编辑游戏…")
        act_del = menu.addAction("🗑 删除游戏")
        act_folder = menu.addAction("📂 打开修改器目录")
        if not any(t.get("dir_path") for t in trainers):
            act_folder.setEnabled(False)
        act = menu.exec(global_pos)
        if act is None:
            return
        if act is act_launch:
            self.launch_game(gid)
        elif act is act_manage:
            self.open_manage(gid)
        elif act is act_edit:
            self.edit_game(gid)
        elif act is act_del:
            self.delete_game(gid)
        elif act is act_folder:
            d = next((t["dir_path"] for t in trainers if t.get("dir_path")), None)
            if d:
                open_folder(d)
        else:
            tid = next((t["id"] for t in trainers
                        if act.text() == f"{t['source']} · {t['name']}"), None)
            if tid:
                self.launch_trainer(gid, tid)

    # ------------------------------------------------------------ 操作
    def add_game(self):
        dlg = AddGameDialog(self._library, self)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

    def edit_game(self, gid):
        game = self._game(gid)
        if not game:
            return
        dlg = EditGameDialog(self._library, gid, self)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

    def delete_game(self, gid):
        game = self._game(gid)
        if not game:
            return
        ret = QMessageBox.question(self, "删除游戏",
                                   f"确定删除「{game['name']}」？\n"
                                   "（仅移除库记录，不删除磁盘文件）",
                                   QMessageBox.Yes | QMessageBox.No,
                                   QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._library.remove_game(gid)
            self._model.reload()
            self._mark_save()

    # ------------------------------------------------------------ Steam 导入
    def import_steam(self):
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "选择包含游戏快捷方式的文件夹")
        if folder:
            self._import_folder = folder
            self._import_was_auto = False
            self._start_import()

    def _maybe_auto_import(self):
        """桌面 game 文件夹有快捷方式且库为空时自动导入（懒加载，不阻塞启动）。"""
        if self._library.all_games():
            return
        for sub in ("game", "游戏", "Games"):
            p = Path.home() / "Desktop" / sub
            if p.is_dir() and any(f.suffix.lower() in (".url", ".lnk") for f in p.iterdir()):
                self._import_folder = p
                self._import_was_auto = True
                QTimer.singleShot(400, self._start_import)
                return

    def _start_import(self):
        task = getattr(self, "_import_task", None)
        if task is not None and task.isRunning():
            return
        folder = getattr(self, "_import_folder", None)
        if not folder:
            return
        dlg = QProgressDialog("正在导入游戏…", "取消", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        self._import_dlg = dlg

        task = _SteamImportWorker(self._library, self._app_info, folder, self)
        task.found_steam.connect(self._import_steam_result)
        task.found_lnk.connect(self._import_lnk_result)
        task.progress.connect(self._import_progress)
        task.finished.connect(self._import_finished)
        task.finished.connect(lambda: self._clear_worker_attr("_import_task", task))
        task.finished.connect(task.deleteLater)   # 结束后释放，防长期积累
        dlg.canceled.connect(task.request_cancel)
        self._import_task = task
        # 批量导入期间挂起整表刷新，结束时一次性 reload（避免 N 次全表重建）
        self._bulk_importing = True
        task.start()

    def _import_progress(self, total, ok):
        dlg = self._import_dlg
        if dlg is not None and dlg.isVisible():
            dlg.setLabelText(f"正在导入游戏…（已识别 {ok}/{total}）")

    def _import_steam_result(self, appid, fallback_name, info):
        # API 拿不到名字时用快捷方式文件名兜底入库，绝不静默丢弃
        name = (info or {}).get("name") or fallback_name
        self._library.add_game(name, steam_id=appid,
                               launch={"type": "steam", "value": appid},
                               cover_url=(info or {}).get("cover_url"))

    def _import_lnk_result(self, name, launch):
        self._library.add_game(name, launch=launch,
                               process_names=[str(launch["value"]).rsplit("\\", 1)[-1]])

    def _import_finished(self, total):
        if self._import_dlg is not None:
            self._import_dlg.close()
            self._import_dlg = None
        self._bulk_importing = False
        self._model.reload()          # 批量完成后一次刷新
        self._mark_save()
        self._schedule_cover_fetch()   # 新导入的无封面游戏补拉封面
        if getattr(self, "_import_was_auto", False):
            self._import_was_auto = False
            return
        if total == 0:
            QMessageBox.information(self, "导入完成", "未找到可识别的游戏快捷方式。")
        else:
            QMessageBox.information(self, "导入完成", f"已导入 {total} 个游戏。")

    # ------------------------------------------------------------ 扫描
    def scan_trainers(self):
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "选择要扫描的文件夹")
        if not folder:
            return
        if getattr(self, "_scanning", False):
            return
        self._scanning = True
        self._scan_error = ""      # 每次新扫描重置，避免继承上次的错误
        import threading
        self._scan_cancel = threading.Event()
        dlg = QProgressDialog("正在扫描修改器…", "取消", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.canceled.connect(lambda: self._scan_cancel.set())

        # 后台线程只收集结果，UI 更新全部走信号回主线程（避免跨线程操作 Qt 对象）
        worker = _ScanWorker(folder, self._scan_cancel, self)
        worker.progress.connect(lambda n: self._scan_progress(dlg, n))
        worker.failed.connect(lambda err: self._scan_failed(dlg, err))
        worker.finished.connect(lambda total: self._scan_done(dlg, worker, total))
        worker.finished.connect(worker.deleteLater)   # 结束后释放
        self._scan_worker = worker
        worker.start()

    def _scan_progress(self, dlg, n):
        if dlg.isVisible():
            dlg.setLabelText(f"已识别 {n} 个候选…")

    def _scan_failed(self, dlg, err):
        self._scan_error = getattr(self, "_scan_error", "") or ""
        if not self._scan_error:
            self._scan_error = err
        if dlg.isVisible():
            dlg.setLabelText(f"扫描遇到错误：{err}")

    def _scan_done(self, dlg, worker, total):
        self._scanning = False
        dlg.close()
        if self._scan_cancel.is_set():
            self._scan_error = ""      # 取消分支也清空，防残留错误影响下次扫描
            return
        err = getattr(self, "_scan_error", "") or ""
        self._scan_error = ""
        results = worker.results
        if err and not results:
            # 扫描遇错且无任何结果：明确告知失败原因，而非显示"未发现修改器"
            QMessageBox.warning(self, "扫描失败",
                                f"扫描过程中发生错误，未获得结果：\n{err}")
            return
        if err and results:
            QMessageBox.warning(self, "扫描部分完成",
                                f"部分目录扫描出错（已忽略）：\n{err}")
        if not results:
            QMessageBox.information(self, "扫描完成", "未发现疑似修改器。")
            return
        scan_dlg = ScanResultDialog(self._library, results, self)
        if scan_dlg.exec():
            self._model.reload()
            self._mark_save()

    # ------------------------------------------------------------ 封面补拉
    _COVER_FETCH_BUDGET = 10           # 每轮最多查询数（防大量游戏刷爆请求）
    _COVER_FAIL_TTL_S = 1800           # 网络失败冷却（30 分钟内不重复请求）
    _COVER_RETRY_MS = 300000           # 周期重试扫描（网络恢复后自动补上）

    def _schedule_cover_fetch(self):
        """为无封面游戏补封面（懒加载，不阻塞 UI）。
        - 所有类型（本地/Steam/Epic）：先**后台线程**离线生成 exe 图标封面；
          找不到图标时用游戏名首字母兜底（生成是重活，丢线程跑，首启不卡）；
        - Steam/Epic：如果还没有 cover_url，再在后台查询官方封面；查到后
          由于加载器优先使用 cover_url，高清官方封面会自动覆盖离线图标。"""
        # 1) 离线封面：后台线程生成，主线程只收结果更新库与卡片。
        #    判断"有没有封面"要看文件是否真的存在：用户删过 data/covers/ 后，
        #    cover_file 字段可能还指向已删除的文件，必须视为无封面重新生成。
        try:
            pending = [g for g in self._library.all_games()
                       if not (g.get("cover_file") and Path(g["cover_file"]).is_file())]
            if pending:
                w = getattr(self, "_offline_cover_worker", None)
                if w is None or not w.isRunning():
                    w = _OfflineCoverWorker(pending, self)
                    w.cover_done.connect(self._offline_cover_done)
                    w.batch_done.connect(self._offline_cover_batch_done)
                    w.finished.connect(w.deleteLater)
                    w.finished.connect(
                        lambda ww=w: self._clear_worker_attr("_offline_cover_worker", ww))
                    self._offline_cover_worker = w
                    w.start()
        except Exception as e:
            audit.warning(f"封面补拉调度异常: {e}")

        # 2) 后台查询官方封面：针对还没有 cover_url 的 Steam/Epic 游戏
        now = time.time()
        fails = getattr(self, "_cover_fail_cache", {})
        games = []
        for g in self._library.all_games():
            if g.get("cover_url"):
                continue
            kind = (g.get("launch") or {}).get("type")
            if kind not in ("steam", "epic"):
                continue
            if now - fails.get(g["id"], 0) < self._COVER_FAIL_TTL_S:
                continue
            # launch_value：Epic 需要从协议 URL 提取 AppName，才能读清单里的
            # DisplayName 辅助搜索官方封面
            launch_value = str((g.get("launch") or {}).get("value") or "")
            games.append((g["id"], g["name"], kind, g.get("steam_id"),
                          launch_value))
            if len(games) >= self._COVER_FETCH_BUDGET:
                break
        if not games:
            return
        if getattr(self, "_cover_fetch_worker", None) is not None \
                and self._cover_fetch_worker.isRunning():
            return
        w = _CoverFetchWorker(games, self)
        w.cover_found.connect(self._cover_fetch_found)
        w.cover_miss.connect(self._cover_fetch_miss)
        w.cover_fail.connect(self._cover_fetch_fail)
        w.finished.connect(lambda: self._clear_worker_attr("_cover_fetch_worker", w))
        w.finished.connect(w.deleteLater)
        self._cover_fetch_worker = w
        w.start()

    def _start_cover_retry_timer(self):
        """周期重试：网络恢复/加速器开启后，无封面游戏会自动补上（无需重启）。"""
        t = QTimer(self)
        t.setInterval(self._COVER_RETRY_MS)
        t.timeout.connect(self._schedule_cover_fetch)
        t.start()
        self._cover_retry_timer = t

    def _offline_cover_done(self, gid, path):
        """后台线程生成好一张离线封面：主线程更新库记录并让卡片立即加载。"""
        if gid and path:
            self._library.update_game(gid, cover_file=path)
            self._covers.forget(gid)
            self._model.cover_updated(gid)

    def _offline_cover_batch_done(self):
        """离线封面整批完成：落盘一次即可。"""
        self._mark_save()

    def _cover_fetch_found(self, gid, url):
        """主线程：把拉到的官方封面 URL 写入游戏记录并刷新卡片。

        这里不再因为已有离线 cover_file 就跳过：cover_url 优先级高于 cover_file，
        写入后封面加载器会自动展示更清晰的官方封面。
        """
        game = self._game(gid)
        if not game or game.get("cover_url"):
            return
        self._library.update_game(gid, cover_url=url)
        # 官方 cover_url 出现，清除旧的失败/兜底缓存，让高清封面立即生效
        self._covers.forget(gid)
        self._model.cover_updated(gid)
        self._mark_save()

    def _cover_fetch_miss(self, gid):
        """确定无结果（API 正常）：长 TTL 负缓存，本会话内不再请求。

        离线图标/首字母封面已在 _schedule_cover_fetch 中生成，所以这里只需要
        登记负缓存，避免反复查询同一款游戏。
        """
        if not hasattr(self, "_cover_neg_cache"):
            self._cover_neg_cache = {}
        self._cover_neg_cache[gid] = time.time()

    def _cover_fetch_fail(self, gid):
        """网络失败（可能被墙/加速器未开）：冷却后周期重试，不污染负缓存。"""
        if not hasattr(self, "_cover_fail_cache"):
            self._cover_fail_cache = {}
        self._cover_fail_cache[gid] = time.time()

    def _clear_worker_attr(self, attr, worker):
        """QThread finished 后清空成员引用，避免对已 deleteLater 对象调 isRunning()。"""
        if getattr(self, attr, None) is worker:
            setattr(self, attr, None)

    # ------------------------------------------------------------ 下载
    def download_trainer(self):
        if not self._library.all_games():
            QMessageBox.information(self, "提示", "请先添加游戏（添加游戏 / 导入 Steam）。")
            return
        dlg = DownloadDialog(self._library, None, self._fling, self._downloader,
                             self._pool, self)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

    # ------------------------------------------------------------ 修改器更新
    def check_trainer_updates(self):
        """手动检查官网下载修改器的最新版本（后台），有新版可一键全部更新。"""
        if not any(t.get("downloaded") for _, t in self._library.all_trainers()):
            QMessageBox.information(
                self, "检查更新",
                "库中没有官网下载的修改器。\n"
                "（仅官网下载的修改器可查更新；手动添加/扫描的没有版本号，无法比较）")
            return
        if getattr(self, "_upd_check_worker", None) is not None \
                and self._upd_check_worker.isRunning():
            return
        self._upd_result = []
        dlg = QProgressDialog("正在检查修改器更新…", "取消", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        w = _UpdateCheckWorker(self._library, self._fling, self)
        w.progress.connect(lambda i, n: self._upd_check_progress(dlg, i, n))
        w.found.connect(self._on_updates_found)
        w.done.connect(lambda n, f: self._upd_check_done(dlg, n, f))
        w.finished.connect(w.deleteLater)
        w.finished.connect(lambda ww=w: self._clear_worker_attr("_upd_check_worker", ww))
        dlg.canceled.connect(w.request_cancel)
        self._upd_check_worker = w
        w.start()

    def _upd_check_progress(self, dlg, i, n):
        if dlg.isVisible():
            dlg.setLabelText(f"正在检查更新…（{i}/{n}）")

    def _on_updates_found(self, items):
        self._upd_result = items

    def _upd_check_done(self, dlg, total, fails):
        dlg.close()
        items = getattr(self, "_upd_result", [])
        if not items:
            if fails:
                QMessageBox.warning(
                    self, "检查更新",
                    f"共检查 {total} 个，其中 {fails} 个查询失败"
                    f"（多为网络问题或页面未匹配），失败原因已写入 data\\audit.log。")
            else:
                QMessageBox.information(
                    self, "检查更新",
                    f"所有修改器均已最新（共检查 {total} 个）")
            return
        lines = "\n".join(
            f"· {it['game']}：v{it['cur'] or '?'} → v{it['new']}" for it in items)
        ret = QMessageBox.question(
            self, "发现新版本",
            f"以下 {len(items)} 个修改器有新版：\n\n{lines}\n\n是否立即全部更新？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._start_update_install(items)
        self._upd_result = []

    def _start_update_install(self, items):
        dlg = QProgressDialog("正在下载更新…", "取消", 0, len(items), self)
        dlg.setWindowModality(Qt.WindowModal)
        w = _UpdateInstallWorker(self._library, self._fling, items, self)
        w.progress.connect(lambda i, n, name: self._upd_install_progress(dlg, i, n, name))
        w.one_done.connect(self._on_trainer_updated)
        w.all_done.connect(lambda ok, f: self._upd_install_done(dlg, ok, f))
        w.finished.connect(w.deleteLater)
        w.finished.connect(lambda ww=w: self._clear_worker_attr("_upd_install_worker", ww))
        dlg.canceled.connect(w.request_cancel)
        self._upd_install_worker = w
        w.start()

    def _upd_install_progress(self, dlg, i, n, name):
        if dlg.isVisible():
            dlg.setValue(i)
            dlg.setLabelText(f"正在更新：{name}（{i}/{n}）")

    def _on_trainer_updated(self, info, item):
        """单个修改器更新完成：刷新记录（版本/路径/哈希），保留首次确认状态；
        同时删除被替换的旧版 exe（否则旧文件残留在文件夹里，页面又不再显示）。"""
        # 记录更新前的旧 exe 路径（用于更新后清理）
        old_exe = ""
        for t in self._library.trainers_of(item["gid"]):
            if t["id"] == item["tid"]:
                old_exe = t.get("exe_path", "") or ""
                break
        self._library.update_trainer(
            item["gid"], item["tid"],
            exe_path=info["exe_path"], dir_path=info["dir_path"],
            version=info.get("version", ""), sha256=info.get("sha256"),
            url=info.get("url", ""))
        audit.info(f"修改器已更新: {item['game']} → v{info.get('version', '')}")
        # 删除旧版文件（新文件同名覆盖时 old==new，跳过）
        new_path = str(Path(info["exe_path"]).resolve())
        if old_exe and str(Path(old_exe).resolve()).casefold() != new_path.casefold():
            try:
                Path(old_exe).unlink(missing_ok=True)
                audit.info(f"更新后清理旧版文件: {old_exe}")
            except OSError as e:
                audit.warning(f"更新后删除旧版文件失败 {old_exe}: {e}")

    def _upd_install_done(self, dlg, ok, fail):
        dlg.close()
        self._model.reload()
        self._mark_save()
        msg = f"更新完成：成功 {ok} 个"
        if fail:
            msg += f"，失败 {fail} 个（网络问题，可稍后再试）"
        QMessageBox.information(self, "更新完成", msg)

    def defender_whitelist(self):
        import subprocess
        root = config.trainers_root
        root.mkdir(parents=True, exist_ok=True)
        ret = QMessageBox.question(
            self, "Defender 白名单",
            f"将修改器库目录加入 Defender 排除项（避免误报拦截）？\n\n"
            f"路径：{root}\n\n"
            "仅建议对你信任的修改器目录操作，确认继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            safe_root = str(root).replace("'", "''")
            ps = ("Add-MpPreference -ExclusionPath '%s'" % safe_root)
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
            QMessageBox.information(self, "完成", "已提交白名单操作。若被杀软拦截请手动确认。")
        except Exception as e:
            QMessageBox.warning(self, "失败", f"添加白名单失败：{e}")

    def open_settings(self):
        # theme_changed：设置页里切换主题时即时重刷主界面样式（不必重启）
        dlg = SettingsDialog(self, theme_changed=self.apply_theme)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

    def apply_theme(self):
        """主题切换后的即时应用：
        1. 重设主窗口 QSS（背景/工具栏/菜单/输入框等）；
        2. 品牌/统计/空状态文字颜色；
        3. 清掉卡片背景缓存（缓存里是旧主题的预渲染图）并重绘；
        4. 重建"无封面"占位图。"""
        self.setStyleSheet(self._style_sheet())
        self._brand_label.setStyleSheet(
            "color: %s; font-size: 15px; font-weight: bold;"
            "padding: 0 14px 0 6px;" % T()["text"])
        self._stats_label.setStyleSheet(
            "color: %s; padding: 0 12px;" % T()["text_dim"])
        self._empty_title.setStyleSheet(
            "color: %s; font-size: 20px; font-weight: bold;" % T()["text"])
        self._empty_desc.setStyleSheet(
            "color: %s; font-size: 13px;" % T()["text_dim"])
        self._delegate._bg_cache.clear()
        self._covers.rebuild_placeholder()
        self._view.viewport().update()
        self._sidebar.viewport().update()

    def refresh_state(self):
        """手动刷新（等效"重启后第一次轮询"，无需重启）：

        1. 清理 process_names 里被误关联的后台常驻进程（node/GameBar/…）；
        2. 清理磁盘上已被删除的修改器记录（prune_missing_trainers——
           用户手动删了 trainers 目录里的文件后，卡片数量要立刻归零，
           不能等重启）；
        3. 立即重扫进程并重新判定"运行中/已停止"；
        4. 重建模型视图并刷新侧边栏计数与状态栏。
        完成后在状态栏给一句反馈。"""
        n = self._proc_watch.prune_background_process_names()
        if n:
            audit.info(f"手动刷新：移除 {n} 个误关联的后台进程名（防运行中误判）")
        # 清理磁盘已删的修改器记录（此前只有启动时清理，刷新按钮必须同步）
        pruned = self._library.prune_missing_trainers()
        if pruned:
            audit.info(f"手动刷新：{pruned} 个修改器文件已不存在，已移除对应记录")
        self._proc_watch.force_poll()
        self._model.reload()
        self._sync_ui_state()
        if n or pruned:
            self._mark_save()
        msg = f"已刷新：清理 {n} 个误关联进程名"
        if pruned:
            msg += f"，移除 {pruned} 条失效修改器记录"
        self.statusBar().showMessage(msg + "，运行状态已重新检测", 3000)

    def refresh_covers(self):
        """手动"刷新封面"按钮：

        1. 清理失效的 cover_file 引用（用户删过 data/covers/ 的场景）；
        2. 清空封面加载器缓存/失败队列；
        3. 清空官方封面的失败冷却与负缓存，立刻重试；
        4. 重新跑 _schedule_cover_fetch：离线封面重新生成 + 官方封面重新查询。
        适合：网络恢复/加速器打开后，一键把没拉到的官方封面补回来。
        """
        stale = _prune_missing_covers(self._library)
        if stale:
            audit.info(f"刷新封面：清理 {stale} 条失效封面引用")
            self._mark_save()
        self._covers.clear_all()
        self._cover_fail_cache = {}
        self._cover_neg_cache = {}
        self._schedule_cover_fetch()
        self._model.reload()
        self._sync_ui_state()
        self.statusBar().showMessage(
            "正在刷新封面：重新生成离线封面 + 重试官方封面…", 4000)

    # ------------------------------------------------------------ 状态
    def _restore_state(self):
        win = config.get("window")
        if win and win.get("w"):
            self.resize(int(win["w"]), int(win["h"]))

    def _save_state(self):
        config.set("window", {"w": self.width(), "h": self.height()})

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            self._proc_watch.set_focused(not self.isMinimized())
        super().changeEvent(e)

    def event(self, e):
        if e.type() == QEvent.ActivationChange:
            self._proc_watch.set_focused(self.isActiveWindow())
        return super().event(e)

    def closeEvent(self, e):
        import time
        self._proc_watch.stop()
        self._covers.shutdown()      # 停止封面重试定时器
        t = getattr(self, "_cover_retry_timer", None)
        if t is not None:
            t.stop()                 # 停止封面补拉周期定时器
        self._do_save()
        self._save_state()
        # 1) 先请求取消所有后台 QThread（导入/卸载检测/扫描）。
        #    HTTP 请求有内置超时且 fetch 会在重试间检查取消
        threads = self.findChildren(QThread)
        for t in threads:
            if hasattr(t, "request_cancel"):
                t.request_cancel()
        # 2) 完整等待线程退出，再关闭共享资源；
        #    慢网络下请求读取可能超过等待上限，超时后跳过 close
        #    （避免在线程仍运行时关闭 Downloader/SteamAppInfo 的资源竞态）
        deadline = time.monotonic() + 60
        timed_out = False
        for t in threads:
            while t.isRunning() and time.monotonic() < deadline:
                t.wait(100)
            if t.isRunning():
                timed_out = True
            t.wait(100)               # 最后再收一次，防竞态
        if timed_out:
            audit.warn("关闭窗口时仍有后台线程未退出，移交孤儿线程接管")
            for t in threads:
                if t.isRunning():
                    _adopt_orphan_thread(t)
        else:
            self._app_info.close()
            self._downloader.close()
        self._pool.waitForDone(5000)
        super().closeEvent(e)

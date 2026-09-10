"""封面加载器：磁盘缓存 + 内存 LRU + 后台任务（从 main_window 拆出）。

职责边界：
- 本模块只管"一张封面怎么显示出来"：磁盘缓存 → 网络官方封面 → 本地封面
  文件（manual- 手选 / offline- 离线兜底）的取图优先级、失败重试（指数退避）、
  任务代数（防旧结果覆盖新请求）；
- "封面文件怎么生成"（exe 图标 / 首字母）在 app.covers 与主窗口的
  离线封面 worker，不属于这里。

新手视角：CoverLoader 是一个自包含的 QObject——对外只有 request/forget/
get/placeholder 几个方法和 cover_ready 信号，与界面零耦合（不 import
任何窗口类），可以单独测试。
"""
import hashlib
import re
import time
from collections import OrderedDict, deque
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

from .. import audit
from ..config import DATA_DIR, config
from ..covers import COVER_W, COVER_H
from ..security import COVER_IMAGE_HOSTS, safe_get
from ..theme import current as T

# 浏览器 User-Agent（与下载页共用 security.BROWSER_UA 的值；此处独立定义
# 是为了避免 security ↔ ui 循环导入——值必须保持一致）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_COVER_LIMIT_MB = 5


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


def cover_hash(gid) -> str:
    """封面文件名哈希段：sha256(gid) 十六进制前 16 位。
    官方缓存（<hash>.png）与 manual-/offline- 前缀命名的唯一来源，
    其他模块（dialogs.save_local_cover、mw_mixins 孤儿清理）一律从这里取。"""
    return hashlib.sha256(str(gid).encode("utf-8")).hexdigest()[:16]


def _cover_cache_path(gid) -> Path:
    """封面缓存文件名：sha256(gid) 十六进制，杜绝恶意/异常 gid 的路径穿越。"""
    return DATA_DIR / "covers" / (cover_hash(gid) + ".png")


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
    """后台线程：按 手选封面 → 磁盘缓存 → 网络官方封面 → 本地封面文件 顺序
    取图片字节，在线程内完成解码/缩放/PNG 落盘（QImage 跨线程安全），
    主线程只做轻量 QPixmap 转换。

    gen：任务代数。forget/重新入队会使旧代数的任务结果作废（见 _on_data）。

    顺序说明：
    - 用户手选的封面（manual- 前缀）= 最高优先级，且不写回缓存；
    - 磁盘缓存是之前下载过的官方封面，其次；
    - 再次尝试 cover_url 网络官方封面；
    - 最后才用本地离线图标/首字母做兜底。
    这样离线生成的 exe 图标不会挡住更高清的官方封面。
    """

    def __init__(self, loader, gid, cover_url, cover_file, disk_path, gen=0):
        super().__init__()
        self.loader = loader
        self.gid = gid
        self.cover_url = cover_url
        self.cover_file = cover_file
        self.disk_path = disk_path
        self.gen = gen

    def run(self):
        limit = _COVER_LIMIT_MB * 1024 * 1024
        data = b""
        source = ""
        # 用户手选的封面（manual- 前缀）= 最高优先级：先于磁盘缓存与官方
        # 网络封面，且不写回缓存。否则官方封面查到后立刻顶掉用户选的图，
        # 下载成功还会把手选文件覆盖掉（manual- 与 gid 键名缓存是两个文件，
        # 但显示层仍会优先 url——所以这里必须在源头就把 manual 放第一）。
        # 手选文件读不到（被删/占用）时降级为普通优先级继续走
        # disk → url → file，绝不能因此判 _FAILED 永久显示"无封面"
        manual = bool(self.cover_file) \
            and Path(self.cover_file).name.startswith("manual-")
        if manual:
            data = _read_limited(Path(self.cover_file), limit)
            if data:
                self._finish(data, "manual")
                return
            audit.warning(f"手选封面文件缺失 gid={self.gid} file={self.cover_file}，"
                          "降级为普通封面优先级")
            manual = False
        # 1) 已下载过的官方封面缓存（手选封面时跳过：缓存是官方图，不该压过手选）
        if not data and not manual and self.disk_path:
            data = _read_limited(Path(self.disk_path), limit)
            if data:
                source = "disk"
        # 2) 网络官方封面（高清，优先于「离线兜底」；手选封面时跳过）
        if not data and not manual and self.cover_url:
            data = safe_get(self.cover_url, COVER_IMAGE_HOSTS, timeout=12,
                            max_hops=4, max_bytes=limit,
                            headers={"User-Agent": _UA},
                            label=f"封面下载 gid={self.gid}")
            if data:
                source = "url"
            else:
                audit.warning(f"封面网络下载失败 gid={self.gid} url={self.cover_url[:100]}")
        # 3) 本地离线图标 / 首字母兜底
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
                # 居中裁剪：KeepAspectRatioByExpanding 溢出的部分两侧/上下均分。
                # 此前从 (0,0) 取左上角——竖版海报（Epic 常见 600x900）扩宽后
                # 只剩顶部 27%，人物/标题全被切掉
                img = img.copy((img.width() - COVER_W) // 2,
                               (img.height() - COVER_H) // 2,
                               COVER_W, COVER_H)
        # 窗口已关闭、加载器已销毁时直接放弃（落盘的缓存下次启动仍有效）；
        # 任务代数过期（中途 forget/重新入队）同样丢弃，防旧结果覆盖新请求。
        # 注意 get 的默认值必须与入队时一致（0），否则 None != 0 会丢弃所有结果
        alive = (self.loader.is_alive()
                 and self.loader._gen.get(self.gid, 0) == self.gen)
        if alive and img is not None and source in ("disk", "url") \
                and self.disk_path:
            # 落盘同样要过代数校验：过期任务把旧图写进磁盘缓存后，
            # 下次启动首屏会短暂显示旧封面（2026-09-10 深度审查 P3-1）
            try:
                img.save(str(self.disk_path), "PNG")
            except OSError:
                pass
        if alive:
            self.loader.data_ready.emit(self.gid, img, source)


class CoverLoader(QObject):
    """封面：磁盘缓存 + 内存真 LRU（有界，OrderedDict）。cover_ready 触发重绘。
    取字节/解码/缩放/落盘全部在线程池，GUI 线程只做轻量 QPixmap 转换。
    下载失败的封面进入自动重试队列：网络恢复/加速器开启后无需重启即可补上。"""
    data_ready = Signal(str, object, str)  # gid, QImage/None, 来源(disk/url/file/"")
    cover_ready = Signal(str)

    # "已确认失败"哨兵：与"尚未加载"（gid 不在 _mem）区分。否则加载失败的
    # 卡片每次重绘都会重新发起注定失败的网络请求，CPU 与网络双重空转
    _FAILED = object()

    _RETRY_INTERVAL_MS = 30000     # 重试扫描周期
    _RETRY_COOLDOWN_S = 120        # 同一 gid 两次尝试的最小间隔
    _RETRY_BUDGET = 8              # 每轮最多重试数（防网络差时刷爆请求）
    _MAX_RETRY_ATTEMPTS = 5        # 自动重试上限（到达后停止，手动刷新封面可重来）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._mem = OrderedDict()     # 真 LRU：move_to_end + popitem(last=False)
        self._inflight = set()
        self._queued = set()          # 已入队（含重试）的 gid，防卡片反复重绘重复入队
        self._retried = set()         # 已因损坏缓存重试过的 gid（防无限循环）
        self._candidates = {}         # gid -> (cover_url, cover_file)，供重试用
        self._gen = {}                # gid -> 任务代数：forget/重新入队时 +1，
                                      # 在飞旧任务的结果按代数作废（防旧结果覆盖新请求）
        self._retry_attempts = {}     # gid -> 已自动重试次数（指数退避用）
        self._queue = deque()         # popleft O(1)：元素 (gid, url, file, disk, gen)
        self._disk = DATA_DIR / "covers"
        try:
            self._disk.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            # 配置被手改坏（非数字/0/负数）不能让程序起不来，也不能让
            # LRU 恒为 0（条目立即弹出 → get 恒 None → 每次重绘重新解码）
            self._limit = max(32, int(config.get("cover_cache_limit")))
        except (TypeError, ValueError):
            self._limit = 200      # 与 config.py 默认值一致
        self._placeholder = self._make_placeholder()
        self._alive = True           # 关窗置 False：后台封面任务不再向已销毁对象发信号
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
        if gid not in self._mem:
            return None                  # 尚未加载 → 调用方应发起 request
        pix = self._mem[gid]
        if pix is self._FAILED:
            return None                  # 已确认失败 → 只走自动重试队列
        self._mem.move_to_end(gid)       # 命中即提升为最近使用
        return pix

    def request(self, gid, cover_url, cover_file):
        # 旧逻辑把内存里的失败标记直接弹掉以"允许重新请求"，但调用方无法
        # 区分"还没加载"和"已失败"，导致每次重绘都重新发起注定失败的请求。
        # 现在：失败态（_FAILED）只有在封面来源发生变化（用户换封面/官方
        # url 刚查到）时才允许重新加载
        if gid in self._mem and self._mem[gid] is self._FAILED:
            last_url, last_file = self._candidates.get(gid, ("", ""))
            if ((cover_url or "") == (last_url or "")
                    and (cover_file or "") == (last_file or "")):
                return
            self._mem.pop(gid, None)
        # 手选封面（manual-）：用户明确选的图 = 最高优先级。必须在「已有加载
        # 即返回」守卫之前处理：清掉内存 / 在飞 / 排队里的旧加载状态（forget
        # 会递增任务代数，防旧任务结果覆盖），然后主线程直接读文件（用户主动
        # 操作，一次性 IO 可接受）。gid 键名的磁盘缓存是官方封面，绝不能
        # 在这里抢先返回顶掉手选图。
        manual = bool(cover_file) and Path(cover_file).name.startswith("manual-")
        if manual:
            self.forget(gid)
            pix = QPixmap(cover_file)
            if not pix.isNull():
                # 与后台路径同等处理：缩放+居中裁剪到封面尺寸。此前直接塞
                # 原图进 LRU——一张 4000x3000 照片约 45MB 常驻内存，
                # 且 paint 非平滑拉伸观感发虚
                pix = pix.scaled(COVER_W, COVER_H,
                                 Qt.KeepAspectRatioByExpanding,
                                 Qt.SmoothTransformation)
                pix = pix.copy((pix.width() - COVER_W) // 2,
                               (pix.height() - COVER_H) // 2,
                               COVER_W, COVER_H)
                self._put(gid, pix)
                self.cover_ready.emit(gid)
                return
            self._candidates[gid] = (cover_url, cover_file)
            self._queued.add(gid)
            self._queue.append((gid, cover_url, cover_file, None,
                                self._gen.get(gid, 0)))
            self._drain()
            return
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
        # 命中缓存的封面也要排队几十秒，滚动体验极差。
        if disk.exists():
            pix = QPixmap(str(disk))
            if not pix.isNull():
                self._put(gid, pix)
                self.cover_ready.emit(gid)
                return
            disk.unlink(missing_ok=True)
        self._candidates[gid] = (cover_url, cover_file)
        self._queued.add(gid)
        self._queue.append((gid, cover_url, cover_file, disk,
                            self._gen.get(gid, 0)))
        self._drain()

    def forget(self, gid):
        """封面来源发生变化时（如离线图标生成后、官方 cover_url 查到后），
        清除该 gid 在加载器里的缓存/排队/失败状态，让下一次请求重新加载。
        同时递增任务代数：此时可能有旧任务仍在跑，它的结果按代数作废——
        否则旧结果会覆盖新请求（卡片卡在占位图直到下一轮重试）。"""
        self._mem.pop(gid, None)
        self._queued.discard(gid)
        self._queue = deque(item for item in self._queue if item[0] != gid)
        self._inflight.discard(gid)
        self._retried.discard(gid)
        self._retry_fail_ts.pop(gid, None)
        self._retry_attempts.pop(gid, None)
        self._retry_pending.discard(gid)
        self._candidates.pop(gid, None)
        self._gen[gid] = self._gen.get(gid, 0) + 1

    def clear_all(self):
        """清空全部封面缓存/排队/失败状态（手动"刷新封面"时用）。
        之后可见卡片重新绘制时会重新发起加载。"""
        self._mem.clear()
        self._queued.clear()
        self._inflight.clear()
        self._retried.clear()
        self._retry_fail_ts.clear()
        self._retry_attempts.clear()
        self._retry_pending.clear()
        self._candidates.clear()
        # _gen 对 可能在飞/排队的 gid 也一并递增：清零会与在飞旧任务的
        # 代数(0)撞车，旧结果穿透校验顶掉新请求（2026-09-04 审查）。
        # 只递增已有键不够——普通请求的 gid 不在 _gen 里
        for gid in set(self._inflight) | set(self._queued) | set(self._gen):
            self._gen[gid] = self._gen.get(gid, 0) + 1
        self._queue.clear()

    def _drain(self):
        while self._queue and len(self._inflight) < self._pool.maxThreadCount():
            gid, cover_url, cover_file, disk, gen = self._queue.popleft()
            self._queued.discard(gid)
            if gid in self._inflight:
                continue
            self._inflight.add(gid)
            self._pool.start(
                _CoverTask(self, gid, cover_url, cover_file, disk, gen=gen))

    @Slot(str, object, str)   # 必须保留：带签名注册后跨线程 queued 分发才可靠
    def _on_data(self, gid, img, source):
        # 结果按任务代数校验：forget/重新入队后旧任务的返回直接丢弃，
        # 防止旧结果覆盖新请求。
        # _drain 放 finally：即使旧代数任务早退，也必须继续派发排队中的
        # 封面——否则队列可能整体停摆
        try:
            if gid not in self._inflight:
                return
            self._inflight.discard(gid)
            self._retry_pending.discard(gid)
            if img is not None:
                # 线程内已解码/缩放，这里只做轻量 QPixmap 转换
                self._put(gid, QPixmap.fromImage(img))
                self.cover_ready.emit(gid)
                self._retry_attempts.pop(gid, None)
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
                # 候选来自离线封面（offline-*）时一并删掉坏文件：离线 worker
                # 只检查"文件是否存在"，不删它会跳过重生成，坏封面永久化
                #（2026-09-10 深度审查 P2-2）
                _cf = (self._candidates.get(gid) or ("", ""))[1]
                if _cf and Path(_cf).name.startswith("offline-"):
                    Path(_cf).unlink(missing_ok=True)
                has_old = (gid in self._mem
                           and self._mem[gid] is not self._FAILED)
                if gid not in self._retried and not has_old:
                    self._retried.add(gid)
                    cover_url, cover_file = self._candidates.get(gid, ("", ""))
                    self._queued.add(gid)
                    # 必须带缓存路径：重试若从网络拿到图，要落盘缓存，
                    # 否则每次启动都重新下载
                    self._queue.append((gid, cover_url, cover_file,
                                        _cover_cache_path(gid),
                                        self._gen.get(gid, 0)))
                else:
                    self._retried.discard(gid)
                    # 已有旧图（如本地封面 + 官方 url 暂不可达）：保留旧图继续
                    # 显示、只登记重试——绝不能把好图弹成"无封面"
                    if not has_old:
                        self._put(gid, self._FAILED)
                        self.cover_ready.emit(gid)
                    self._retry_fail_ts[gid] = time.time()
        finally:
            # 有任务完成即空出一个并发位：继续派发排队中的封面
            self._drain()

    def _retry_scan(self):
        """定时扫描失败封面：超过冷却时间后重新发起加载（网络恢复后自动补上）。
        冷却随尝试次数指数退避；已有旧图的保留旧图，拿到新图再替换。"""
        now = time.time()
        budget = self._RETRY_BUDGET
        for gid in list(self._retry_fail_ts):
            if budget <= 0:
                break
            if gid in self._inflight or gid in self._retry_pending:
                continue
            attempts = self._retry_attempts.get(gid, 0)
            if attempts >= self._MAX_RETRY_ATTEMPTS:
                # 达到上限：放弃自动重试（手动"刷新封面"会清空计数重来），
                # 防止注定失败的请求长期占用线程池
                self._retry_fail_ts.pop(gid, None)
                continue
            cooldown = self._RETRY_COOLDOWN_S * (2 ** min(attempts, 4))
            if now - self._retry_fail_ts[gid] < cooldown:
                continue
            budget -= 1
            self._retry_attempts[gid] = attempts + 1
            self._retry_pending.add(gid)   # 防本登记在任务完成前被重复入队
            cover_url, cover_file = self._candidates.get(gid, ("", ""))
            self._queued.add(gid)
            self._queue.append((gid, cover_url, cover_file,
                                _cover_cache_path(gid), self._gen.get(gid, 0)))
            self._drain()

    def shutdown(self, wait_ms=5000):
        """关闭窗口时停止重试定时器，等待在飞封面任务收尾（解码/落盘/
        发信号），再停止发信号。这里等的是加载器自己的线程池——主窗口
        关窗流程只管理它自己 findChildren 到的 QThread。"""
        self._retry_timer.stop()
        try:
            self._pool.waitForDone(wait_ms)
        except RuntimeError:
            pass                   # 加载器 C++ 对象已销毁（关窗竞态）
        self._alive = False

    def is_alive(self):
        return self._alive

    def _put(self, gid, pix):
        self._mem[gid] = pix
        self._mem.move_to_end(gid)
        while len(self._mem) > self._limit:
            self._mem.popitem(last=False)

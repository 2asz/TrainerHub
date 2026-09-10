"""官网下载对话框（风灵月影源）+ 两个后台 worker。"""
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from ... import audit
from ...config import config
from ...downloader.base import DownloadError
from ._common import _StyledDialog, trainer_dest_dir


# 下载并发名额（download_concurrency）：模块级单例——此前每个对话框实例各建
# 一个信号量，一次只开一个下载对话框时限制永远不生效；更新安装链路另有串行
# worker 不经过这里。配置改动需重启应用生效（与 poll_interval 等一致）
try:
    _DL_SEM = threading.BoundedSemaphore(
        max(1, int(config.get("download_concurrency"))))
except (TypeError, ValueError):
    _DL_SEM = threading.BoundedSemaphore(2)


# ================================================================ 下载
class _AutoUpdateProbeWorker(QThread):
    """后台探测下载页的 Auto-Updating Version 链接（网络请求不卡 UI）。"""
    found = Signal(str)             # 链接或空串

    def __init__(self, adapter, page_url, parent=None):
        super().__init__(parent)
        self._adapter = adapter
        self._page_url = page_url

    def run(self):
        try:
            self.found.emit(self._adapter.latest_autoupdate_link(self._page_url) or "")
        except Exception:
            self.found.emit("")


class _DownloadWorker(QThread):
    """后台任务：search（搜索）/ resolve（解析版本）/ install（下载安装）。"""
    search_done = Signal(list)
    search_fail = Signal(str)
    resolve_done = Signal(str, list)     # page_url, 版本条目列表
    resolve_fail = Signal(str, str)      # page_url, 错误
    progress = Signal(int, int)
    install_done = Signal(dict)
    install_fail = Signal(str)

    def __init__(self, adapter, downloader, mode, query=None, page_url=None,
                 game_name=None, dest_root=None, entry=None, parent=None):
        super().__init__(parent)
        self._adapter = adapter
        self._downloader = downloader
        self._mode = mode
        self._query = query
        self._page_url = page_url
        self._game_name = game_name
        self._dest_root = dest_root
        self._entry = entry              # 用户选定的版本条目（None=最新）
        self._stop = threading.Event()
        self._owns_sem = False           # install 模式：由 UI acquire、本线程 finally 归还

    def request_cancel(self):
        self._stop.set()

    def run(self):
        if self._mode == "search":
            try:
                self.search_done.emit(self._adapter.search(self._query))
            except Exception as e:
                self.search_fail.emit(str(e))
        elif self._mode == "resolve":
            try:
                entries = self._adapter.resolve_downloads(self._page_url)
                if not self._stop.is_set():
                    self.resolve_done.emit(self._page_url, entries)
            except Exception as e:
                if not self._stop.is_set():
                    self.resolve_fail.emit(self._page_url, str(e))
        else:
            # 并发名额的归还必须在 worker 线程的 finally 里：放 UI 回调的话，
            # 「下载中关闭对话框」→ 信号被孤儿接管断开/接收者析构 → 无人归还，
            # 模块级单例信号量会被永久占死（2026-09-02 审查 P2-1）
            try:
                info = self._adapter.install(
                    self._game_name, self._page_url, self._dest_root,
                    progress_cb=self._on_progress, cancel=self._stop,
                    entry=self._entry)
                self.install_done.emit(info)
            except DownloadError as e:
                self.install_fail.emit(str(e))
            except Exception as e:
                self.install_fail.emit(f"未知错误: {e}")
            finally:
                if self._owns_sem:
                    _DL_SEM.release()

    def _on_progress(self, done, total):
        self.progress.emit(done, total)


class DownloadDialog(_StyledDialog):
    """下载修改器（风灵月影源）。失败时可降级为复制下载链接。"""

    def __init__(self, library, game_id, adapter, downloader, parent=None):
        super().__init__(parent, "下载修改器", 720, 560)
        self._library = library
        self._adapter = adapter
        self._downloader = downloader
        self._worker = None
        self._last_url = None

        # 游戏选择
        self._game_combo = QComboBox()
        for g in library.all_games():
            self._game_combo.addItem(g["name"], g["id"])
        if game_id:
            idx = self._game_combo.findData(game_id)
            if idx >= 0:
                self._game_combo.setCurrentIndex(idx)

        self._search_btn = QPushButton("搜索官网")
        self._search_btn.setObjectName("primary")
        self._search_btn.clicked.connect(self._search)
        self._status = QLabel("")
        self._status.setWordWrap(True)

        self._results = QListWidget()
        self._results.itemSelectionChanged.connect(self._on_select)

        # 版本选择：选中搜索结果后自动解析该页全部版本（首个标注「最新」）
        # 解析结果按页面缓存：重复选中不再请求（Cloudflare 会拦快速重复访问）
        self._resolved_cache = {}
        self._version_row = QWidget()
        vh = QHBoxLayout(self._version_row)
        vh.setContentsMargins(0, 0, 0, 0)
        vh.addWidget(QLabel("版本："))
        self._version_combo = QComboBox()
        self._version_combo.currentIndexChanged.connect(self._on_version_changed)
        vh.addWidget(self._version_combo, 1)
        self._refresh_ver_btn = QPushButton("🔄")
        self._refresh_ver_btn.setFixedWidth(34)
        self._refresh_ver_btn.setToolTip("重新解析版本（绕过缓存，页面更新后使用）")
        self._refresh_ver_btn.clicked.connect(self._force_resolve)
        vh.addWidget(self._refresh_ver_btn)
        self._version_row.setVisible(False)
        self._entries = []            # 当前解析到的版本条目
        self._resolve_worker = None
        self._autoupd_worker = None

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)

        self._dl_btn = QPushButton("下载并入库")
        self._dl_btn.setObjectName("primary")
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self._download)
        self._copy_btn = QPushButton("复制下载链接")
        self._copy_btn.setEnabled(False)
        self._copy_btn.clicked.connect(self._copy_url)
        # 官网「Auto-Updating Version」自更新存根的链接（探测到才显示）
        self._autoupd_url = ""
        self._autoupd_btn = QPushButton("复制自动更新版链接")
        self._autoupd_btn.setVisible(False)
        self._autoupd_btn.clicked.connect(self._copy_autoupd)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)

        # 来源域名显式展示（仿冒站克隆了官方页面结构，用户核对域名是
        # 最直接的一道防线；域名来自适配器的白名单类属性）
        hosts = "、".join(getattr(self._adapter, "ALLOWED_HOSTS", ())
                          or ("flingtrainer.com",))
        src_label = QLabel(f"来源：{hosts}")
        src_label.setObjectName("detailDim")

        top = QHBoxLayout()
        top.addWidget(QLabel("游戏："))
        top.addWidget(self._game_combo, 1)
        top.addWidget(self._search_btn)

        row = QHBoxLayout()
        row.addWidget(self._dl_btn)
        row.addWidget(self._copy_btn)
        row.addWidget(self._autoupd_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self._status)
        lay.addWidget(src_label)
        lay.addWidget(self._results, 1)
        lay.addWidget(self._version_row)
        lay.addWidget(self._progress)
        lay.addLayout(row)

    # ---- 搜索 ----
    def _search(self):
        gid = self._game_combo.currentData()
        game = self._library.get_game(gid)
        if not game:
            return
        self._set_busy(True, "正在搜索官网…")
        self._results.clear()
        worker = _DownloadWorker(self._adapter, self._downloader, "search",
                                 query=game["name"], parent=self)
        worker.search_done.connect(self._on_search_done)
        worker.search_fail.connect(self._on_search_fail)
        worker.finished.connect(worker.deleteLater)   # 结束后释放
        self._worker = worker
        worker.start()

    def _on_search_done(self, results):
        self._set_busy(False, "")
        if not results:
            self._status.setText("未在官网找到结果，可尝试修改游戏名后重试，或使用「手动添加」。")
            return
        for r in results:
            it = QListWidgetItem(r["title"])
            it.setData(Qt.UserRole, r["page_url"])
            it.setToolTip(r["page_url"])
            self._results.addItem(it)
        self._status.setText(f"找到 {len(results)} 个结果，双击选中后点击下载。")

    def _on_search_fail(self, err):
        self._set_busy(False, f"搜索失败：{err}\n可手动复制官网链接添加，或使用「手动添加」。")

    def _on_select(self):
        has = bool(self._results.selectedItems())
        self._dl_btn.setEnabled(has)
        # 修复：复制按钮随选中启用（版本解析前复制页面链接，解析后复制直链）
        self._copy_btn.setEnabled(has)
        if has:
            self._resolve_versions(self._results.selectedItems()[0].data(Qt.UserRole))
        else:
            self._version_row.setVisible(False)
            self._entries = []

    # ---- 版本解析 ----
    def _resolve_versions(self, page_url, force=False):
        """选中搜索结果后解析该页全部版本（后台，可取消）。
        成功过的页面走缓存，不再重复请求——Cloudflare 对快速重复访问
        会返回 403（这正是"第一次能用第二次不行"的原因）。"""
        self._last_url = page_url       # 兜底：解析完成前复制的是页面链接
        old = self._resolve_worker
        if old is not None:
            try:
                if old.isRunning():
                    old.request_cancel()
            except RuntimeError:
                pass            # 线程已结束并 deleteLater（成员引用未及清理）
            self._resolve_worker = None
        if not force and page_url in self._resolved_cache:
            self._on_resolve_done(page_url, self._resolved_cache[page_url])
            return
        self._version_row.setVisible(False)
        self._entries = []
        self._status.setText("正在解析可用版本…")
        w = _DownloadWorker(self._adapter, self._downloader, "resolve",
                            page_url=page_url, parent=self)
        w.resolve_done.connect(self._on_resolve_done)
        w.resolve_fail.connect(self._on_resolve_fail)
        w.finished.connect(w.deleteLater)
        # 结束后清引用：防止后续访问已 deleteLater 的 C++ 对象
        w.finished.connect(lambda ww=w: self._clear_resolve_worker(ww))
        self._resolve_worker = w
        w.start()

    def _clear_resolve_worker(self, w):
        if self._resolve_worker is w:
            self._resolve_worker = None

    def _force_resolve(self):
        """🔄 按钮：绕过缓存强制重新解析当前选中页面的版本。"""
        cur = self._results.selectedItems()
        if cur:
            self._resolve_versions(cur[0].data(Qt.UserRole), force=True)

    def _on_resolve_done(self, page_url, entries):
        # 选择已切换则丢弃过期结果（异步竞态防护）
        cur = self._results.selectedItems()
        if not cur or cur[0].data(Qt.UserRole) != page_url:
            return
        self._resolved_cache[page_url] = entries or []
        self._entries = entries or []
        if not self._entries:
            self._status.setText("该页面未解析出可用版本，可复制页面链接手动下载。")
            return
        self._version_combo.blockSignals(True)
        self._version_combo.clear()
        used_bases = set()
        for i, e in enumerate(self._entries):
            base = f"v{e['version']}" if e.get("version") \
                else (e.get("name") or "未知版本")
            if base in used_bases:
                # 标签仍重复（同名同版本镜像等）：附加 URL token 尾缀区分线路
                token = (e.get("url", "").rsplit("/", 1)[-1] or "?")[:4]
                base = f"{base}（线路 {token}）"
            used_bases.add(base)
            label = base + ("（最新）" if i == 0 else "")
            self._version_combo.addItem(label, i)
            self._version_combo.setItemData(
                i, f"{e.get('name', '')}\n{e.get('url', '')}", Qt.ToolTipRole)
        self._version_combo.setCurrentIndex(0)
        self._version_combo.blockSignals(False)
        self._version_row.setVisible(True)
        self._on_version_changed(0)
        self._status.setText(
            f"解析到 {len(self._entries)} 个版本，默认最新，可下拉自选。")
        # 异步探测官网的「Auto-Updating Version」自更新存根（不自动入库，
        # 只把链接交给用户——存根需联网自更新，sha256 不稳定不适合版本比对）
        self._autoupd_btn.setVisible(False)
        self._autoupd_url = ""
        worker = _AutoUpdateProbeWorker(self._adapter, page_url, self)
        worker.found.connect(self._on_autoupd_found)
        worker.finished.connect(worker.deleteLater)
        self._autoupd_worker = worker
        worker.start()

    def _on_autoupd_found(self, url):
        if not url:
            return
        self._autoupd_url = url
        self._autoupd_btn.setVisible(True)
        self._autoupd_btn.setEnabled(True)
        self._status.setText(self._status.text()
                             + " · 官网另有自动更新版（选项更多，需联网自更新）")

    def _copy_autoupd(self):
        if self._autoupd_url:
            QApplication.clipboard().setText(self._autoupd_url)
            self._status.setText("自动更新版链接已复制（到浏览器打开下载，安装后自动保持最新）。")

    def _on_resolve_fail(self, page_url, err):
        cur = self._results.selectedItems()
        if not cur or cur[0].data(Qt.UserRole) != page_url:
            return
        self._status.setText(
            f"版本解析失败：{err}\n可直接下载（自动取最新）或复制页面链接。")

    def _on_version_changed(self, idx):
        """切换版本：复制按钮指向该版本直链。"""
        entries = getattr(self, "_entries", None) or []
        if 0 <= idx < len(entries):
            self._last_url = entries[idx].get("url") or self._last_url

    # ---- 下载 ----
    def _download(self):
        sel = self._results.selectedItems()
        if not sel:
            return
        page_url = sel[0].data(Qt.UserRole)
        self._last_url = page_url       # 兜底：失败时也能复制页面链接
        gid = self._game_combo.currentData()
        game = self._library.get_game(gid)
        if not game:
            return
        # download_concurrency 并发限制：占用名额失败则提示稍候（不阻塞 UI）
        if not _DL_SEM.acquire(blocking=False):
            QMessageBox.information(
                self, "下载繁忙",
                "当前已在进行多个下载，请稍候再试（可在设置中调整并发数）。")
            return
        try:
            entry = None
            entries = getattr(self, "_entries", None) or []
            idx = self._version_combo.currentIndex()
            if entries and 0 <= idx < len(entries):
                entry = entries[idx]       # 用户选定的版本
            self._start_download(game, page_url, entry)
        except Exception:
            # worker 未接管名额（还没 start 成功）才由这里归还；
            # 启动成功后由 worker.run 的 finally 归还
            _DL_SEM.release()
            raise

    def _start_download(self, game, page_url, entry=None):
        # 重复下载复用原目录（同一游戏不再新建 "名字 (2)"）
        try:
            dest = trainer_dest_dir(game, self._library, self._adapter.SOURCE)
        except (RuntimeError, OSError) as e:
            # worker 未接管名额：这里必须归还，否则目录不可写的场景每点
            # 一次"下载"就烧掉一个并发名额（2026-09-04 审查 P1）
            _DL_SEM.release()
            QMessageBox.warning(self, "无法下载", str(e))
            return
        ver = (entry or {}).get("version", "")
        self._set_busy(True, f"正在下载{' v' + ver if ver else ''}…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._worker = _DownloadWorker(
            self._adapter, self._downloader, "install",
            page_url=page_url, game_name=game["name"], dest_root=dest,
            entry=entry, parent=self)
        self._worker._owns_sem = True   # 名额移交 worker：run 的 finally 里归还
        self._worker.progress.connect(self._on_progress)
        self._worker.install_done.connect(self._on_install_done)
        self._worker.install_fail.connect(self._on_install_fail)
        self._worker.finished.connect(self._worker.deleteLater)   # 结束后释放
        self._worker.start()

    def _on_progress(self, done, total):
        if total > 0:
            self._progress.setValue(int(done * 100 / max(total, 1)))
            self._status.setText(f"下载中 {done // 1024} KB / {total // 1024} KB…")

    def _on_install_done(self, info):
        # 并发名额已由 worker.run 的 finally 归还（不再在 UI 回调里 release：
        # 对话框被孤儿接管/销毁时回调可能不执行，名额会被永久占死）
        self._set_busy(False, "")
        self._progress.setVisible(False)
        gid = self._game_combo.currentData()
        game = self._library.get_game(gid)
        if not game:
            # 下载期间游戏被删：文件已落盘但不入库，避免写进悬空记录
            QMessageBox.warning(
                self, "下载完成",
                f"文件已下载，但所选游戏已被删除，未入库：\n{info['exe_path']}")
            self.reject()
            return
        # 同一游戏重复下载同版本：更新既有记录而不是 append 重复条目
        dup = next((t for t in game.get("trainers", [])
                    if t.get("exe_path") == info["exe_path"]), None)
        if dup is not None:
            self._library.update_trainer(
                gid, dup["id"], version=info.get("version", ""),
                sha256=info["sha256"], downloaded=True,
                update_available=False, first_run_confirmed=False)
            audit.info(f"重复下载，已更新记录: {info['exe_path']}")
            QMessageBox.information(
                self, "下载完成",
                f"该修改器已在库中，记录已更新：\n{info['exe_path']}")
            self.accept()
            return
        name = f"[{self._adapter.SOURCE}]《{game['name']}》修改器"
        self._library.add_trainer(
            gid, source=self._adapter.SOURCE, name=name,
            exe_path=info["exe_path"], dir_path=info["dir_path"],
            version=info.get("version", ""), sha256=info["sha256"], downloaded=True)
        self._last_url = info.get("url", "")
        audit.info(f"官网下载入库: {info['exe_path']} sha256={info['sha256']}")
        QMessageBox.information(
            self, "下载完成",
            f"已下载并入库：\n{info['exe_path']}\n\n"
            f"SHA-256: {info['sha256']}\n\n"
            "首次启动该修改器时需确认（安全策略）。")
        self.accept()

    def _on_install_fail(self, err):
        # 名额已由 worker.run 的 finally 归还（不在 UI 回调里重复 release）
        self._set_busy(False, f"下载失败：{err}")
        self._progress.setVisible(False)

    def _copy_url(self):
        if self._last_url:
            QApplication.clipboard().setText(self._last_url)
            self._status.setText("链接已复制到剪贴板。")

    def _set_busy(self, busy, text):
        self._status.setText(text)
        self._search_btn.setEnabled(not busy)
        self._dl_btn.setEnabled(not busy and bool(self._results.selectedItems()))
        self._game_combo.setEnabled(not busy)

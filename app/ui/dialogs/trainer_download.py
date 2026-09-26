"""官网下载对话框（风灵月影源）+ 两个后台 worker。"""
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

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
    found = Signal(str, str)        # 链接或空串, 发起探测的 page_url

    def __init__(self, adapter, page_url, parent=None):
        super().__init__(parent)
        self._adapter = adapter
        self._page_url = page_url
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()            # worker 契约：关窗收尾可请求取消

    def run(self):
        if self._stop.is_set():
            return
        try:
            url = self._adapter.latest_autoupdate_link(self._page_url) or ""
        except Exception:
            url = ""
        if not self._stop.is_set():
            self.found.emit(url, self._page_url)


class _DownloadWorker(QThread):
    """后台任务：search（搜索）/ resolve（解析版本）/ install（下载安装）。"""
    search_done = Signal(list, str)      # 结果, 实际生效的查询词
    search_fail = Signal(str)
    resolve_done = Signal(str, list, list)   # page_url, 版本条目, 手动渠道
    resolve_fail = Signal(str, str)      # page_url, 错误
    progress = Signal(int, int)
    install_done = Signal(dict)
    install_fail = Signal(str)

    def __init__(self, adapter, downloader, mode, query=None, page_url=None,
                 game_name=None, dest_root=None, entry=None, alt_query=None,
                 parent=None):
        super().__init__(parent)
        self._adapter = adapter
        self._downloader = downloader
        self._mode = mode
        self._query = query
        self._page_url = page_url
        self._game_name = game_name
        self._dest_root = dest_root
        self._entry = entry              # 用户选定的版本条目（None=最新）
        # 备用查询词提供者（可调用对象，后台线程内执行）：主查询无结果时
        # 调用它取英文名重试（风灵站标题是英文，中文游戏名搜不到）
        self._alt_query = alt_query
        self._stop = threading.Event()
        self._owns_sem = False           # install 模式：由 UI acquire、本线程 finally 归还

    def request_cancel(self):
        self._stop.set()

    def run(self):
        if self._mode == "search":
            used, err = self._query, None
            results = []
            try:
                results = self._adapter.search(used)
            except Exception as e:
                err = str(e)          # 首次失败也允许走英文名重试（P3-2）
            if not results and self._alt_query is not None \
                    and not self._stop.is_set():
                # 中文名无结果/失败时用英文名重试一次（Steam 游戏；
                # 2026-09-13 用户反馈：剑星 / Stellar Blade）
                alt = ""
                try:
                    alt = (self._alt_query() or "").strip()
                except Exception:
                    pass
                if alt and alt.casefold() != (self._query or "").casefold():
                    try:
                        r2 = self._adapter.search(alt)
                        if r2:
                            results, used = r2, alt
                    except Exception as e:
                        err = err or str(e)
            if results:
                self.search_done.emit(results, used)
            elif err:
                self.search_fail.emit(err)
            else:
                self.search_done.emit([], used)
        elif self._mode == "resolve":
            try:
                entries = self._adapter.resolve_downloads(self._page_url)
                manual = []
                if not entries and not self._stop.is_set() \
                        and hasattr(self._adapter, "manual_links"):
                    # 自动直链解析为空（如 MediaFire 文件被删）：把详情页的
                    # 手动下载渠道（网盘链接等）带给用户（2026-09-13 用户反馈）
                    try:
                        manual = self._adapter.manual_links(self._page_url)
                    except Exception:
                        manual = []
                if not self._stop.is_set():
                    self.resolve_done.emit(self._page_url, entries, manual)
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
    """下载修改器（多来源：风灵月影 / 小幸）。失败时可降级为复制下载链接。"""

    def __init__(self, library, game_id, adapters, downloader, parent=None):
        """adapters: {来源名: TrainerDownloader 实例}（保持传入顺序作下拉序）。"""
        super().__init__(parent, "下载修改器", 720, 560)
        self._library = library
        self._adapters = dict(adapters)
        self._adapter = next(iter(self._adapters.values()))
        self._downloader = downloader
        self._worker = None
        self._last_url = None
        self._search_source = None     # 发起搜索时的来源（回调校验防串源）

        # 游戏选择（限宽：内容通常很短，拉满整行会显得空旷笨重）
        self._game_combo = QComboBox()
        self._game_combo.setMinimumWidth(260)
        self._game_combo.setMaximumWidth(360)
        for g in library.all_games():
            self._game_combo.addItem(g["name"], g["id"])
        if game_id:
            idx = self._game_combo.findData(game_id)
            if idx >= 0:
                self._game_combo.setCurrentIndex(idx)

        # 下载来源（多源时显示下拉；切换清空上一源的结果与缓存）
        self._source_combo = QComboBox()
        for name in self._adapters:
            self._source_combo.addItem(name)
        if len(self._adapters) > 1:
            self._source_combo.currentTextChanged.connect(
                self._on_source_changed)
        else:
            self._source_combo.setVisible(False)

        self._search_btn = QPushButton("搜索官网")
        self._search_btn.setObjectName("primary")
        self._search_btn.clicked.connect(self._search)
        # 关键词（默认当前游戏名，可改成英文名——风灵站标题为英文，中文名
        # 搜不到时用户可自填，如「剑星」→ Stellar Blade）
        self._kw_edit = QLineEdit()
        self._kw_edit.setMinimumWidth(260)
        self._kw_edit.setMaximumWidth(360)
        self._kw_edit.setMaxLength(80)     # 防超长查询拼进 URL（P3-1）
        self._kw_edit.setPlaceholderText("中文搜不到时试试英文名")
        self._kw_edit.returnPressed.connect(self._search)
        self._game_combo.currentIndexChanged.connect(self._on_game_changed)
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
        # 最直接的一道防线；域名来自适配器的白名单类属性，切来源时更新）
        self._src_label = QLabel("")
        self._src_label.setObjectName("detailDim")
        self._update_src_label()
        self._on_game_changed()      # 关键词预填当前游戏名

        top = QHBoxLayout()
        top.addWidget(QLabel("游戏："))
        top.addWidget(self._game_combo)
        if len(self._adapters) > 1:
            top.addWidget(QLabel("来源："))
            top.addWidget(self._source_combo)
        top.addStretch(1)                 # 控件限宽左对齐，不再拉满整行
        kw_row = QHBoxLayout()
        kw_row.addWidget(QLabel("关键词："))
        kw_row.addWidget(self._kw_edit)
        kw_row.addWidget(self._search_btn)
        kw_row.addStretch(1)

        row = QHBoxLayout()
        row.addWidget(self._dl_btn)
        row.addWidget(self._copy_btn)
        row.addWidget(self._autoupd_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(kw_row)
        lay.addWidget(self._status)
        lay.addWidget(self._src_label)
        lay.addWidget(self._results, 1)
        lay.addWidget(self._version_row)
        lay.addWidget(self._progress)
        lay.addLayout(row)

    def _on_game_changed(self, _idx=None):
        """切换游戏：关键词跟随填入该游戏名（用户可再手改成英文名）。"""
        g = self._library.get_game(self._game_combo.currentData())
        self._kw_edit.setText(g["name"] if g else "")

    def _update_src_label(self):
        hosts = "、".join(getattr(self._adapter, "ALLOWED_HOSTS", ()) or ())
        self._src_label.setText(f"来源：{hosts}")

    def _on_source_changed(self, name):
        """切换来源：清空上一源的结果/版本/缓存。在跑的搜索 worker 结果
        回来时由 _on_search_done/_on_search_fail 的串源守卫丢弃并复位
        busy；版本解析结果有 page_url 校验天然丢弃（选中项已清空）。"""
        if name not in self._adapters or self._adapters[name] is self._adapter:
            return
        self._adapter = self._adapters[name]
        self._update_src_label()
        self._results.clear()
        self._version_row.setVisible(False)
        self._entries = []
        self._resolved_cache.clear()
        self._dl_btn.setEnabled(False)
        self._copy_btn.setEnabled(False)
        self._autoupd_btn.setVisible(False)
        self._autoupd_url = ""
        self._status.setText(f"已切换到「{name}」来源，点「搜索官网」开始。")

    # ---- 搜索 ----
    def _search(self):
        gid = self._game_combo.currentData()
        game = self._library.get_game(gid)
        if not game:
            return
        # 重入防护：搜索按钮在 busy 时被禁用，但回车（returnPressed）仍会
        # 触发本方法——再起一个 worker 会结果串台（2026-09-13 审查 P1）
        if not self._search_btn.isEnabled():
            return
        query = self._kw_edit.text().strip() or game["name"]
        self._search_query = query
        self._set_busy(True, f"正在用「{query}」搜索「{self._adapter.SOURCE}」官网…")
        self._results.clear()
        self._search_source = self._adapter.SOURCE
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        seq = self._search_seq
        # Steam 游戏：中文名无结果时自动用官方英文名重试。仅在
        # ①适配器需要（风灵站标题为英文）②用户未手改关键词（query 仍是
        # 游戏名——手改后自动换词会让用户误以为结果匹配所填关键词，
        # 2026-09-13 审查 P2-5/P3-3）时启用
        alt_query = None
        sid = str(game.get("steam_id") or "")
        # kw_edit 有 maxLength(80)：游戏名超长时 setText 被截断，比较须用
        # 同样截断后的原名，否则英文名自动重试被静默关闭（2026-09-13 审查 P3）
        if getattr(self._adapter, "NEEDS_ENGLISH_NAME", False) \
                and query == game["name"][:self._kw_edit.maxLength()] \
                and sid.isdigit() \
                and (game.get("launch") or {}).get("type") == "steam":
            from ...steam_import import get_app_info
            alt_query = (lambda s=sid: get_app_info().fetch_english_name(s))
        worker = _DownloadWorker(self._adapter, self._downloader, "search",
                                 query=query, alt_query=alt_query,
                                 parent=self)
        worker.search_done.connect(
            lambda r, q, s=seq: self._on_search_done(r, q, s))
        worker.search_fail.connect(self._on_search_fail)
        worker.finished.connect(worker.deleteLater)   # 结束后释放
        self._worker = worker
        worker.start()

    def _on_search_done(self, results, used_query="", seq=None):
        # 过期结果（新一轮搜索已发起）与串源迟到结果都直接丢弃——但**必须
        # 先复位 busy**：串源守卫原先在复位之前 return，来源切换后迟到的
        # 搜索结果会让对话框永久卡在"搜索中"（按钮/下拉全部禁用，
        # 2026-09-13 审查 P1）
        if self._search_source != self._adapter.SOURCE:
            self._set_busy(False, "")
            self._status.setText(f"已忽略「{self._search_source}」来源的迟到结果。")
            return
        if seq is not None and seq != self._search_seq:
            self._set_busy(False, "")
            return
        self._set_busy(False, "")
        typed = getattr(self, "_search_query", "") or ""
        if not results:
            if used_query and used_query != typed:
                # 已经用英文名重试过：别再提示"改成英文名"（P2-4）
                self._status.setText(
                    f"用「{typed}」和英文名「{used_query}」都没找到结果。"
                    "可在上方换个关键词，或使用「手动添加」。")
            elif getattr(self._adapter, "NEEDS_ENGLISH_NAME", False):
                self._status.setText(
                    "未找到结果。风灵站修改器标题为英文，中文名可能搜不到——"
                    "可在上方把「关键词」改成英文名再搜（如 剑星 → Stellar Blade），"
                    "或使用「手动添加」。")
            else:
                # 中文站（小幸）：按源给文案，不再硬编码"风灵站"（终轮 P3-1）
                self._status.setText(
                    f"未在「{self._adapter.SOURCE}」找到结果——"
                    "可在上方换个关键词（或用英文名）再搜，或使用「手动添加」。")
            return
        for r in results:
            it = QListWidgetItem(r["title"])
            it.setData(Qt.UserRole, r["page_url"])
            it.setToolTip(r["page_url"])
            self._results.addItem(it)
        if used_query and used_query != typed:
            # 重试成功要说明关键词被替换过（否则用户以为结果来自所填关键词）
            self._status.setText(
                f"「{typed}」无结果，已自动用英文名「{used_query}」搜到 "
                f"{len(results)} 个结果，双击选中后点击下载。")
        else:
            self._status.setText(f"找到 {len(results)} 个结果，双击选中后点击下载。")

    def _on_search_fail(self, err):
        # 与 _on_search_done 同款串源守卫：旧源超时的迟到失败不应覆盖
        # 新源的状态/解锁按钮（2026-09-13 审查 P3）
        if self._search_source != self._adapter.SOURCE:
            self._set_busy(False, "")
            return
        self._set_busy(False, f"搜索失败：{err}\n可手动复制官网链接添加，或使用「手动添加」。")

    def _on_select(self):
        items = self._results.selectedItems()
        has = bool(items)
        if not has:
            self._dl_btn.setEnabled(False)
            self._copy_btn.setEnabled(False)
            self._version_row.setVisible(False)
            self._entries = []
            return
        page_url = items[0].data(Qt.UserRole)
        # 手动渠道条目（UserRole 为空、User+1 存网盘链接）：
        # MediaFire 分享页 → 「下载并入库」可尝试程序内下载（install 自动
        # 解析直链；文件已被网盘删除时明确报错）；其他网盘 → 仅复制链接
        manual_url = items[0].data(Qt.UserRole + 1)
        if not page_url and manual_url:
            downloadable = "mediafire.com" in manual_url
            self._dl_btn.setEnabled(downloadable)
            self._dl_btn.setText("下载并入库" if downloadable else "复制链接")
            self._copy_btn.setEnabled(True)
            self._last_url = manual_url
            self._version_row.setVisible(False)
            self._entries = []
            if downloadable:
                self._status.setText(
                    "MediaFire 渠道：点「下载并入库」尝试在程序内下载"
                    "（若文件已被网盘删除会明确提示）。")
            else:
                self._status.setText(
                    "该渠道无法在程序内下载：点「复制链接」到浏览器下载并解压后，"
                    "用详情面板「➕ 添加」入库。")
            return
        self._dl_btn.setEnabled(True)
        self._dl_btn.setText("下载并入库")
        # 修复：复制按钮随选中启用（版本解析前复制页面链接，解析后复制直链）
        self._copy_btn.setEnabled(True)
        self._resolve_versions(page_url)

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
            self._on_resolve_done(page_url, *self._resolved_cache[page_url])
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

    def _on_resolve_done(self, page_url, entries, manual=()):
        # 选择已切换则丢弃过期结果（异步竞态防护）
        cur = self._results.selectedItems()
        if not cur or cur[0].data(Qt.UserRole) != page_url:
            return
        self._resolved_cache[page_url] = (entries or [], manual or ())
        self._entries = entries or []
        if not self._entries:
            self._results.clear()
            if manual:
                # 自动直链失效（如 MediaFire 文件被删）：展示详情页提供的手动
                # 渠道（2026-09-13 用户反馈「战神4 下载不了」）
                for m in manual:
                    it = QListWidgetItem(f"【手动】{m['name']}")
                    it.setData(Qt.UserRole, None)
                    it.setData(Qt.UserRole + 1, m["url"])
                    it.setToolTip(m["url"])
                    self._results.addItem(it)
                self._copy_btn.setEnabled(False)
                self._dl_btn.setEnabled(False)
                self._status.setText(
                    "该页面的自动下载已不可用（网盘文件可能已被删除）。"
                    "下方为页面提供的手动下载渠道，选中后「复制下载链接」。")
                return
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
        # 只把链接交给用户——存根需联网自更新，sha256 不稳定不适合版本比对）。
        # 仅风灵源提供该探测（latest_autoupdate_link）；其他来源跳过
        self._autoupd_btn.setVisible(False)
        self._autoupd_url = ""
        if hasattr(self._adapter, "latest_autoupdate_link"):
            worker = _AutoUpdateProbeWorker(self._adapter, page_url, self)
            worker.found.connect(
                lambda u, pu=page_url: self._on_autoupd_found(u, pu))
            worker.finished.connect(worker.deleteLater)
            self._autoupd_worker = worker
            worker.start()

    def _on_autoupd_found(self, url, page_url):
        # 选中结果已切换则丢弃：慢网络下 probe A 迟到返回会把 _autoupd_url
        # 写成别的游戏的下载入口（2026-09-13 审查 P2）
        cur = self._results.selectedItems()
        if not cur or cur[0].data(Qt.UserRole) != page_url:
            return
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
        # 手动渠道条目（UserRole 为空）：MediaFire 分享页可尝试程序内
        # 下载（install 会自动解析直链；文件已被网盘删除时会明确报错），
        # 其他网盘（百度网盘等）无法程序化，只允许复制链接
        manual_url = sel[0].data(Qt.UserRole + 1)
        if not page_url and manual_url:
            if "mediafire.com" not in manual_url:
                QApplication.clipboard().setText(manual_url)
                self._status.setText(
                    "该渠道（百度网盘等）无法在程序内下载，链接已复制到剪贴板，"
                    "请到浏览器下载并解压后用「➕ 添加」入库。")
                return
            page_url = manual_url
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
        # 捕获安装时刻的归属（游戏/来源）：下载耗时期间用户可能切来源/换
        # 游戏，入库时必须用捕获值而不是对话框"当前"状态，否则会把风灵的
        # 文件记成小幸、把修改器挂到错误游戏名下（2026-09-13 审查 P1）
        self._install_ctx = {"gid": game["id"], "source": self._adapter.SOURCE}
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
        # 用安装时刻捕获的归属（切来源/换游戏后对话框"当前"状态已不可信，
        # 2026-09-13 审查 P1）
        ctx = getattr(self, "_install_ctx", {}) or {}
        gid = ctx.get("gid") or self._game_combo.currentData()
        source = ctx.get("source") or self._adapter.SOURCE
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
        name = f"[{source}]《{game['name']}》修改器"
        self._library.add_trainer(
            gid, source=source, name=name,
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
        # 回车（returnPressed）绕过按钮禁用的第二触发点：关键词框与来源
        # 下拉在 busy 时同样禁用（2026-09-13 审查 P1）
        self._kw_edit.setEnabled(not busy)
        self._source_combo.setEnabled(not busy)

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
import time
from pathlib import Path

# 关闭窗口时未及时退出的后台线程：脱离父对象并保持强引用，
# 避免 QThread 在运行中被销毁导致崩溃（Qt 崩溃点）；进程退出前最后等待一次
_ORPHAN_THREADS = []


# 孤儿线程需要断开的自定义信号名单（finished 上的释放连接单独重接）。
# PySide6 没有"断开全部信号"的无参重载（QObject.disconnect() 无参抛 TypeError，
# 2026-09-02 审查实测），只能按名字逐个来；名单外漏掉的信号靠 Qt 在接收者
# 析构时自动断连兜底，不会崩。
_ORPHAN_SIGNAL_NAMES = (
    "progress", "found", "done", "one_done", "all_done", "import_finished",
    "scan_finished", "found_steam", "found_lnk", "cover_done", "batch_done",
    "cover_found", "cover_miss", "cover_fail", "search_done", "search_fail",
    "resolve_done", "resolve_fail", "install_done", "install_fail",
)


def _adopt_orphan_thread(t):
    """接管未退出的后台线程：断开父子归属，线程结束后自动释放并 deleteLater。"""
    for name in _ORPHAN_SIGNAL_NAMES:
        sig = getattr(t, name, None)
        if sig is not None:
            try:
                sig.disconnect()       # 断开该信号上的全部连接（含指向主窗口的槽）
            except (RuntimeError, TypeError):
                pass                   # 本来就没连接 / C++ 对象已析构
    try:
        t.finished.disconnect()        # 同样先清空 finished，再重接释放钩子
    except (RuntimeError, TypeError):
        pass
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

from PySide6.QtCore import QEvent, QEventLoop, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QMainWindow, QMenu, QMessageBox,
                               QSizePolicy, QSplitter, QStackedWidget, QToolBar,
                               QWidget, QVBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QHBoxLayout,
                               QPushButton)

from .. import audit
from ..config import config, SOURCES, APP_VERSION
from ..theme import current as T, load_from_config
from ..library import Library
from ..steam_import import SteamAppInfo
from ..process_watch import ProcessWatch
from ..launcher import (launch_steam_game, launch_file, launch_protocol,
                        launch_trainer, open_folder)
from ..downloader.base import Downloader
from ..downloader.fling import FlingTrainerDownloader
from ..install_info import game_install_roots
from .dialogs import (AddGameDialog, EditGameDialog, DownloadDialog,
                      SettingsDialog)
from .detail_panel import DetailPanel, PANEL_W
from .card_view import (GameListModel, CardDelegate, SidebarDelegate,
                        CardView)
from .cover_loader import CoverLoader
from .mw_mixins import IoMixin, UpdateMixin
from .tasks import _StartupCleanWorker

class MainWindow(IoMixin, UpdateMixin, QMainWindow):
    def __init__(self, library: Library):
        super().__init__()
        self._library = library
        # 先按配置恢复主题，再创建 UI 相关对象——封面占位图、对话框样式
        # 都按当前主题生成，顺序反了浅色主题首启会闪深色占位图
        load_from_config()
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
        # 下载/更新 worker 全部是自带生命周期的 QThread（父对象统一回收），
        # 不再需要额外的 QThreadPool（旧 _pool 从未 start() 过任何任务，纯死代码）
        self._import_dlg = None
        self._import_task = None
        self._import_was_auto = False
        self._current_cat = "全部"
        # 关窗收尾中：弹窗类槽（如更新确认）直接放弃，防重入起新线程
        self._closing = False

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

        self._build_ui()
        self._connect()
        self._restore_state()
        self._proc_watch.start()
        self._save_timer.start()
        self._maybe_auto_import()
        # 自动启动修改器（设置开启时）：游戏进程出现即启动对应修改器
        self._proc_watch.about_to_change.connect(self._auto_launch_trainer)
        # 游戏时长统计：进程出现记会话起点、退出时累加 play_seconds
        self._play_sessions = {}          # gid -> 会话开始时间戳
        self._proc_watch.about_to_change.connect(self._on_play_session)
        # 启动 2 秒后为无封面的 Steam/Epic 游戏补拉官方封面（懒加载）。
        # singleShot 带接收者 self：窗口销毁后定时器自动失效——不带接收者
        # 的话关窗后照常触发，2s 这个会起新的后台 worker，无人能取消
        QTimer.singleShot(2000, self, self._schedule_cover_fetch)
        self._start_cover_retry_timer()   # 周期重试：网络恢复后自动补上
        # 启动 6 秒后静默检查修改器更新（有新版 → 状态栏提示，不弹窗）
        QTimer.singleShot(6000, self, self._silent_update_check)
        # 启动 10 秒后清理孤儿封面缓存（低频，不阻塞）
        QTimer.singleShot(10000, self, self._cleanup_orphan_covers)
        # 库文件损坏被恢复/无法恢复时明确告知用户（不能静默）。
        # singleShot(0)：等事件循环启动、主窗 show() 之后再弹，
        # 否则模态框出现在空桌面上
        QTimer.singleShot(0, self, self._warn_library_state)

    def _warn_library_state(self):
        """启动时检查游戏库的加载状态：从备份恢复了要告知，彻底失败更要告知。"""
        lib = self._library
        if getattr(lib, "load_note", None):
            audit.warning(f"游戏库损坏恢复: {lib.load_note}")
            QMessageBox.warning(self, "游戏库已恢复", lib.load_note
                                + "\n\n建议检查游戏与修改器列表是否完整。")
        elif lib.last_error:
            audit.warning(f"游戏库加载失败: {lib.last_error}")
            QMessageBox.warning(self, "游戏库加载失败",
                                lib.last_error + "\n\n"
                                "本次将以空库启动；确认磁盘后可重启重试。")

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self):
        # 窗口标题带上应用版本号（发布后用户一眼可辨识版本，反馈问题好对齐）
        self.setWindowTitle(f"Trainer Hub v{APP_VERSION} · 修改器整合")
        self.resize(1280, 800)   # 默认尺寸；_restore_state 再按配置覆盖
        self.setMinimumSize(980, 620)   # 卡片墙至少 2 列 + 侧边栏
        self.setAcceptDrops(True)       # 支持拖拽 .exe/.lnk/.url 导入游戏
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

        # 右侧详情面板（单击卡片滑出；启动时隐藏）
        self._detail = DetailPanel(self._library, self._covers, self)
        self._detail.setObjectName("detailPanel")
        self._detail.hide()

        # 三栏布局：侧边栏 | 卡片墙 | 详情面板（面板不参与拉伸，卡片墙吃剩余空间）
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._stack)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setCollapsible(2, False)   # 防误拖拽把面板压成 0 宽（有 ✕ 收起按钮）
        splitter.setHandleWidth(1)
        splitter.setSizes([self._sidebar.width(),
                           max(420, self.width() - self._sidebar.width() - PANEL_W),
                           PANEL_W])
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

        # 搜索框：紧跟操作按钮。注意必须放在弹性 spacer **之前**——
        # 窗口不够宽时 Qt 会把工具栏末尾的控件折叠进 » 扩展按钮，
        # 搜索框放最后就会"消失"（点 » 才能找到）
        self._search_box = QLineEdit(self)
        self._search_box.setPlaceholderText("🔎  搜索游戏…（Ctrl+F）")
        self._search_box.setMinimumWidth(190)
        self._search_box.setMaximumWidth(320)
        self._search_box.setClearButtonEnabled(True)
        self._search_box.setObjectName("searchBox")
        tb.addWidget(self._search_box)

        # 弹性透明占位：吃掉搜索框右侧的剩余宽度。
        # 必须显式声明透明——主样式表 "QWidget { background }" 会把它
        # 染成一块与工具栏不同色的"空白按钮"，看起来像误加的控件
        spacer = QWidget(self)
        spacer.setObjectName("toolbarSpacer")
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # 统计信息移到底部状态栏：工具栏只留操作，信息归状态栏（分层更清晰）
        self._stats_label = QLabel("", self)
        self._stats_label.setStyleSheet(
            "color: %s; padding: 0 12px;" % T()["text_dim"])
        self.statusBar().addWidget(self._stats_label)

        # 更新提示（状态栏右侧，有新版才显示；点击弹出更新确认）
        self._upd_hint = QLabel(self)
        self._upd_hint.setTextFormat(Qt.RichText)
        self._upd_hint.setStyleSheet(
            f"color: {T()['accent']}; padding: 0 12px; font-weight: bold;")
        self._upd_hint.setVisible(False)
        self._upd_hint.linkActivated.connect(self._upd_hint_clicked)
        self.statusBar().addPermanentWidget(self._upd_hint)

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
        # 分组分隔线：来源组首个（"风灵月影"）与筛选组（"无修改器"）之前
        sep_before = {SOURCES[0], "无修改器"} if SOURCES else {"无修改器"}
        for key in self._cat_list():
            it = QListWidgetItem()
            it.setText(key)                 # 无障碍/辅助技术仍可读到名称
            it.setData(Qt.UserRole, key)
            if key in sep_before:
                it.setData(SidebarDelegate.Role_Sep, True)
            self._sidebar.addItem(it)
            self._cat_keys.append(key)
            self._cat_items[key] = it
        self._sidebar.setCurrentRow(0)
        self._sidebar.currentRowChanged.connect(self._on_cat_changed)

    def _cat_list(self):
        cats = ["全部", "运行中", "最近游玩"]
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
        QWidget#toolbarSpacer {{ background: transparent; }}
        /* ---------- 右侧详情面板 ---------- */
        QWidget#detailPanel {{ background: {t['sidebar']}; }}
        QWidget#detailPanel QLabel {{ background: transparent; }}
        QLabel#detailName {{ font-size: 15px; font-weight: bold; }}
        QLabel#detailDim {{ color: {t['text_dim']}; font-size: 12px; }}
        QLabel#detailPath {{ color: {t['text_dim']}; font-size: 11px; }}
        QLabel#detailBadgeRunning {{ color: {t['running']}; font-size: 12px;
                                    font-weight: bold; }}
        QFrame#detailSep {{ background: {t['border']}; border: none;
                           max-height: 1px; min-height: 1px; }}
        QListWidget#detailTrainers {{ background: transparent;
                                     border: 1px solid {t['border']};
                                     border-radius: 8px; }}
        QListWidget#detailTrainers::item {{ border-bottom: 1px solid {t['border']}; }}
        QWidget#trainerRow {{ background: transparent; }}
        QPushButton#detailBtn {{ background: {t['btn']};
                                border: 1px solid {t['btn_border']};
                                border-radius: 5px; padding: 3px 8px;
                                color: {t['text']}; font-size: 12px; }}
        QPushButton#detailBtn:hover {{ background: {t['btn_hover']};
                                      border-color: {t['accent']}; }}
        QPushButton#detailBtn:pressed {{ background: {t['btn_pressed']}; }}
        QPushButton#detailPrimary {{ background: {t['accent']}; border: none;
                                    border-radius: 6px; color: white;
                                    font-weight: bold; padding: 7px 16px; }}
        QPushButton#detailPrimary:hover {{ background: {t['primary_hover']}; }}
        QPushButton#detailPrimary:pressed {{ background: {t['accent_dark']};
                                            padding: 8px 16px 6px 16px; }}
        """

    def _connect(self):
        # 新手视角：这里把"信号"接到"槽"。信号是 Qt 的事件通知（例如
        # 卡片按钮被点击、游戏进程出现），connect 表示"事件一发生就调用这个函数"：
        # 例如双击卡片 → cardDoubleClicked → launch_game（启动游戏）
        self._view.cardDoubleClicked.connect(self.launch_game)
        self._view.gameBtnClicked.connect(self.launch_game)
        self._view.trainerBtnClicked.connect(self._trainer_btn)
        self._view.rightClicked.connect(self._context_menu)
        self._view.deleteRequested.connect(self.delete_game)
        # 选中态变化由 Qt 自动重绘，无需整表 repaint
        # 详情面板：单击卡片（或方向键移动选中）→ 右侧滑出该游戏详情
        self._view.selectionModel().currentChanged.connect(self._on_current_changed)
        self._view.cardClicked.connect(self._show_detail)   # 点已选中的卡片也要能唤出面板
        self._detail.launchGameRequested.connect(self.launch_game)
        self._detail.launchTrainerRequested.connect(self.launch_trainer)
        self._detail.editGameRequested.connect(self.edit_game)
        self._detail.deleteGameRequested.connect(self.delete_game)
        self._detail.checkTrainerUpdateRequested.connect(self.check_trainer_updates_for)
        self._detail.openTrainerFolderRequested.connect(self._open_trainer_folder)
        self._detail.downloadTrainerRequested.connect(self._download_for_game)
        self._detail.trainersChanged.connect(self._on_detail_trainers_changed)
        self._search_box.textChanged.connect(lambda _: self._search_timer.start())
        self._proc_watch.running_changed.connect(self._model.set_running)
        # 运行状态变化 → 侧边栏「运行中」计数即时刷新 + 详情面板徽章
        self._proc_watch.running_changed.connect(self._detail.set_running)
        self._proc_watch.running_changed.connect(lambda *_: self._refresh_cat_counts())
        self._covers.cover_ready.connect(self._model.cover_updated)
        self._covers.cover_ready.connect(self._detail.on_cover)
        self._model.modelReset.connect(self._sync_ui_state)
        # 快捷键：Ctrl+F 聚焦搜索；搜索框内 Esc 清空
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

    # ------------------------------------------------------------ 详情面板
    def _show_detail(self, gid):
        """唤出右侧详情面板并展示该游戏（面板已显示则就地刷新内容）。
        程序性重选（_sync_detail_selection）只刷新内容，不把隐藏的面板拉出来。"""
        if not gid:
            return
        if not getattr(self, "_reselecting", False) and not self._detail.isVisible():
            self._detail.setVisible(True)
        self._detail.show_game(gid)

    def _on_current_changed(self, cur, _prev):
        """卡片墙选中项变化（单击/方向键）：详情面板跟着切换。"""
        if cur.isValid():
            self._show_detail(cur.data(GameListModel.Role_GameId))

    def _sync_detail_selection(self):
        """模型 reload 后 Qt 会清空卡片选中：让选中态跟随面板当前游戏
        （游戏还在列表里时）。面板没在展示时不动选择。"""
        gid = self._detail.current_gid
        if not gid:
            return
        # 标记"程序性重选"：setCurrentIndex 触发的 currentChanged 不应
        # 把已收起的面板重新弹出（否则点一次刷新面板就自己滑出来）
        self._reselecting = True
        try:
            for i in range(self._model.rowCount()):
                idx = self._model.index(i, 0)
                if idx.data(GameListModel.Role_GameId) == gid:
                    self._view.setCurrentIndex(idx)
                    return
        finally:
            self._reselecting = False

    def _on_detail_trainers_changed(self, gid):
        """面板里添加/移除了修改器：刷新模型 + 保存（面板自身经 modelReset 刷新）。"""
        self._model.reload()
        self._mark_save()

    def _open_trainer_folder(self, gid, tid):
        t = next((x for x in self._library.trainers_of(gid)
                  if x["id"] == tid), None)
        if not t:
            return
        if t.get("dir_path"):
            open_folder(t["dir_path"])
        else:
            open_folder(str(Path(t["exe_path"]).parent))

    def _download_for_game(self, gid):
        """详情面板「⬇️ 官网」：打开下载对话框并预选该游戏。"""
        if not self._require_game(gid):
            return
        dlg = DownloadDialog(self._library, gid, self._fling, self._downloader, self)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

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
        # 详情面板跟随数据刷新（展示中的游戏被删 → 面板自动隐藏）
        self._detail.refresh()
        self._sync_detail_selection()

    def _refresh_cat_counts(self, games=None):
        if games is None:
            games = self._library.all_games()
        # 单遍统计：此前每个分类各扫一遍全库（O(N×K)），运行状态每次变化都触发
        by_source = {}
        n_recent = n_no_trainer = 0
        for g in games:
            for s in {t.get("source") for t in g.get("trainers", [])}:
                by_source[s] = by_source.get(s, 0) + 1      # 按游戏数计，不是修改器数
            if g.get("last_played"):
                n_recent += 1
            if not g.get("trainers"):
                n_no_trainer += 1
        for key, it in self._cat_items.items():
            if key == "全部":
                n = len(games)
            elif key == "运行中":
                n = self._model.running_count()
            elif key == "最近游玩":
                n = n_recent
            elif key == "无修改器":
                n = n_no_trainer
            else:
                n = by_source.get(key, 0)
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
            QMessageBox.warning(
                self, "保存失败",
                msg + "\n（本次会话不再重复提示）\n\n"
                "若程序放在 Program Files 等受保护目录，请解压到普通文件夹"
                "（如桌面）后重试；也可在设置里检查数据目录。")

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
                    exe = Path(str(launch["value"])).name
                    if exe.lower().endswith(".exe"):
                        pns = list(game.get("process_names") or [])
                        if exe not in pns:
                            pns.append(exe)
                            self._library.update_game(gid, process_names=pns)
                            self._mark_save()
            else:
                QMessageBox.information(
                    self, "无法启动", "该游戏未配置启动方式，请在「编辑」中设置。")
                return
            self._library.touch_played(gid)   # 游玩记录（最近游玩分类/排序用）
            self._mark_save()
            self._detail.refresh_if_showing(gid)   # 面板正展示该游戏 → 统计即时更新
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
            # 采样"启动后新出现"的进程。快照现在是 {pid: 进程名}（Toolhelp，
            # 毫秒级）；exe 只对**新增 pid** 懒查（psutil 单次 ~1ms），
            # 不再全量扫（旧实现全量 psutil ~680ms，点启动卡一下）
            if after is None:
                after = self._proc_watch.capture_processes()
            for pid in set(after) - set(before):
                item = (self._proc_watch.query_exe(pid), after[pid])
                if item not in new:
                    new.append(item)

        def _apply():
            # 7s 最后一次采样后，只关联"最后一次采样时仍存活"的进程——
            # 启动瞬间被拉起的临时进程（安装器/更新器/崩溃报告等）往往
            # 几秒内自己退出，误关联它们会让游戏"已退出却仍显示运行中"
            after = self._proc_watch.capture_processes()
            _sample(after)
            alive = set(after.values())
            keep = [item for item in new if item[1] in alive]
            # 只关联游戏安装目录内的进程（Steam installdir / 本地启动 exe 目录 /
            # Epic 清单位置）——后台常驻软件即使同时被拉起也不会被误记成游戏进程
            game = self._game(gid)
            roots = game_install_roots(game) if game else []
            self._proc_watch.associate_processes(gid, keep, roots=roots)
            if self._library.is_dirty():
                self._mark_save()

        # 接收者传 self：主窗口销毁后定时器不再触发（否则回调访问已销毁
        # 的窗口成员会 RuntimeError）
        QTimer.singleShot(3000, self, _sample)
        QTimer.singleShot(7000, self, _apply)

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

    def _on_play_session(self, gid, running):
        """游戏时长会话计时：进程出现记起点，退出时把时长累加进库。
        用 about_to_change（先于 running_changed），保证库已更新后再刷 UI。"""
        if running:
            self._play_sessions[gid] = time.time()
            return
        started = self._play_sessions.pop(gid, None)
        if started is None:
            return
        self._library.add_play_seconds(gid, time.time() - started)
        self._mark_save()
        self._detail.refresh_if_showing(gid)

    def _flush_play_sessions(self):
        """退出前把未结束的游玩会话按时长落库（按当前时刻结算）。"""
        for gid, started in list(self._play_sessions.items()):
            self._library.add_play_seconds(gid, time.time() - started)
        self._play_sessions.clear()

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
                                     self._downloader, self)
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
        """「管理修改器」入口：已升级为右侧常驻详情面板（取代旧弹窗），
        面板里可 启动/更新/打开目录/移除/添加 修改器。"""
        if not self._require_game(gid):
            return
        self._show_detail(gid)

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
            # 新游戏大概率还没有封面：后台补齐（提取图标/查官方封面都在
            # 线程池里做，不在点击"确定"的瞬间卡界面）
            self._schedule_cover_fetch()

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
    def download_trainer(self):
        if not self._library.all_games():
            QMessageBox.information(self, "提示", "请先添加游戏（添加游戏 / 导入 Steam）。")
            return
        dlg = DownloadDialog(self._library, None, self._fling, self._downloader,
                             self)
        if dlg.exec():
            self._model.reload()
            self._mark_save()

    # ------------------------------------------------------------ 修改器更新
    # ------------------------------------------------------------ 设置 / 主题 / 刷新
    def defender_whitelist(self):
        root = config.trainers_root
        ret = QMessageBox.question(
            self, "Defender 白名单",
            f"将修改器库目录加入 Defender 排除项（避免误报拦截）？\n\n"
            f"路径：{root}\n\n"
            "仅建议对你信任的修改器目录操作，确认继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        from ..defender import defender_add_exclusion
        ok, err = defender_add_exclusion(root)
        if not ok:
            QMessageBox.warning(
                self, "失败",
                "添加白名单未成功（可能需要以管理员身份运行本程序）。\n"
                f"{err[:200]}")
            audit.warning(f"Defender 白名单失败 {err[:200]}")
            return
        audit.info(f"Defender 白名单已提交: {root}")
        QMessageBox.information(self, "完成", "已提交白名单操作。若被杀软拦截请手动确认。")

    def open_settings(self):
        # theme_changed：设置页里切换主题时即时重刷主界面样式（不必重启）
        dlg = SettingsDialog(self._library, self, theme_changed=self.apply_theme)
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
        self._delegate.retheme()   # 刷新 delegate 缓存的主题色并清背景预渲染
        self._covers.rebuild_placeholder()
        self._upd_hint.setStyleSheet(
            f"color: {T()['accent']}; padding: 0 12px; font-weight: bold;")
        self._view.viewport().update()
        self._sidebar.viewport().update()
        # 详情面板的行/徽章是按主题色重建的，切换主题后整体重绘；
        # 占位图是旧主题画的，清守卫强制面板换新占位图
        self._detail.retheme()

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

    # ------------------------------------------------------------ 状态
    def _restore_state(self):
        """恢复窗口尺寸（唯一实现，_build_ui 里的 resize 只提供默认值）。
        配置被手改坏（缺失/非数字）时忽略尺寸项，不能让启动失败。"""
        try:
            win = config.get("window") or {}
            w, h = int(win.get("w") or 0), int(win.get("h") or 0)
            if w > 0 and h > 0:
                self.resize(w, h)
        except Exception:
            # 配置手改坏（window 变字符串等）不能让启动失败
            pass

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
        self._closing = True           # 收尾期间弹窗类槽直接放弃（防重入）
        self._flush_play_sessions()      # 未结束的游玩会话按时长落库
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
        # 2) 等待线程退出（期间处理事件，界面不假死；总上限 10 秒）。
        #    超时的线程移交孤儿接管（断开父子、结束自发释放），共享资源
        #    不再 close——避免在线程仍运行时关闭 Downloader/SteamAppInfo 的竞态
        def _alive(t) -> bool:
            # processEvents 会投递排队的 finished → deleteLater，C++ 对象
            # 可能在循环中途被析构；此后 isRunning/wait 会抛 RuntimeError
            try:
                return t.isRunning()
            except RuntimeError:
                return False

        deadline = time.monotonic() + 10
        timed_out = False
        for t in threads:
            while _alive(t) and time.monotonic() < deadline:
                try:
                    t.wait(100)
                except RuntimeError:
                    break
                # 屏蔽键鼠输入但照常派发信号/定时器：不处理事件 UI 全程假死，
                # 不屏蔽输入则完成槽可能在此重入（弹"立即更新"再起新线程，
                # 该线程不在 threads 名单里，窗口析构时直接 abort）
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            if _alive(t):
                timed_out = True
            try:
                t.wait(100)               # 最后再收一次，防竞态
            except RuntimeError:
                pass
        if timed_out:
            audit.warn("关闭窗口时仍有后台线程未退出，移交孤儿线程接管")
            for t in threads:
                if _alive(t):
                    _adopt_orphan_thread(t)
        else:
            self._app_info.close()
            self._downloader.close()
        super().closeEvent(e)

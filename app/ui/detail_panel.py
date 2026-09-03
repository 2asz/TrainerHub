"""右侧详情面板：单击卡片常驻显示——大封面 + 游玩统计 + 启动方式 + 修改器完整列表。

新手视角：
- 这是从「管理修改器」弹窗升级来的常驻面板，与卡片墙并排放在主窗口的
  QSplitter 里（侧边栏 | 卡片墙 | 本面板）；
- 面板是"纯展示 + 轻操作"组件：数据直接读 Library，但凡涉及库的整体变化
  （增删修改器）只发信号给 MainWindow（控制器），由它统一负责模型刷新与
  保存——界面与数据依旧互不纠缠；
- 修改器行的"启动"也走主窗口的 launch_trainer（那里有官网修改器首次运行
  的安全确认逻辑），面板不重复实现。
"""
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..utils import rel_time as _rel_time   # 通用工具（原从 card_view 导入）
from .dialogs import AddTrainerDialog

PANEL_W = 356                    # 面板初始宽度（QSplitter 里可拖动调整）


class DetailPanel(QWidget):
    """主窗口右侧滑出的游戏详情栏。当前展示的游戏见 current_gid（None=隐藏中）。"""

    # 面板只发"意图"，动作由主窗口执行（启动/更新等涉及确认框与全局状态）
    launchGameRequested = Signal(str)
    launchTrainerRequested = Signal(str, str)
    editGameRequested = Signal(str)
    deleteGameRequested = Signal(str)
    checkTrainerUpdateRequested = Signal(str, str)
    openTrainerFolderRequested = Signal(str, str)
    downloadTrainerRequested = Signal(str)   # 官网下载该游戏的修改器
    trainersChanged = Signal(str)   # 面板内增/删了修改器 → 主窗口刷新模型并保存

    def __init__(self, library, covers, parent=None):
        super().__init__(parent)
        self._library = library
        self._covers = covers       # CoverLoader：内存封面缓存（get/request）
        self._gid = None            # 当前展示的游戏 id
        self._running = set()       # 运行中游戏 id 集合（与卡片模型同步维护）
        self._last_cover_w = 0      # 上次渲染封面用的宽度（防拖动时反复缩放）
        self._last_cover_gid = None  # 上次渲染封面的游戏（切换游戏必须重渲染）
        self._tr_sig = None         # 修改器列表内容签名（不变则不重建行控件）
        self._build()

    @property
    def current_gid(self):
        return self._gid

    # ------------------------------------------------------------ UI 构建
    def _build(self):
        self.setMinimumWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        # 头部：游戏名 + 运行中徽章 + 关闭按钮
        head = QHBoxLayout()
        head.setSpacing(6)
        self._name = QLabel("", self)
        self._name.setObjectName("detailName")
        self._badge = QLabel("● 运行中", self)
        self._badge.setObjectName("detailBadgeRunning")
        self._badge.setVisible(False)
        btn_close = QPushButton("✕", self)
        btn_close.setObjectName("detailBtn")
        btn_close.setFixedSize(26, 24)
        btn_close.setToolTip("收起详情面板")
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.clicked.connect(self.hide)
        head.addWidget(self._name, 1)
        head.addWidget(self._badge)
        head.addWidget(btn_close)
        lay.addLayout(head)

        # 大封面（宽度随面板伸缩，等比缩放不变形）
        self._cover = QLabel("", self)
        self._cover.setObjectName("detailCover")
        self._cover.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._cover)

        # 游玩统计 + 启动方式 + 启动按钮
        self._stats = QLabel("", self)
        self._stats.setObjectName("detailDim")
        lay.addWidget(self._stats)

        launch_row = QHBoxLayout()
        launch_row.setSpacing(8)
        self._launch_info = QLabel("", self)
        self._launch_info.setObjectName("detailDim")
        self._btn_launch = QPushButton("▶ 启动游戏", self)
        self._btn_launch.setObjectName("detailPrimary")
        self._btn_launch.setCursor(Qt.PointingHandCursor)
        self._btn_launch.setFocusPolicy(Qt.NoFocus)
        self._btn_launch.clicked.connect(
            lambda: self.launchGameRequested.emit(self._gid))
        launch_row.addWidget(self._launch_info, 1)
        launch_row.addWidget(self._btn_launch)
        lay.addLayout(launch_row)

        sep = QFrame(self)
        sep.setObjectName("detailSep")
        sep.setFrameShape(QFrame.HLine)
        lay.addWidget(sep)

        # 修改器区：标题 + 官网下载/添加按钮 + 完整列表
        tr_head = QHBoxLayout()
        self._tr_title = QLabel("修改器", self)
        self._tr_title.setObjectName("detailName")
        self._btn_dl = QPushButton("⬇️ 官网", self)
        self._btn_dl.setObjectName("detailBtn")
        self._btn_dl.setToolTip("从风灵月影官网搜索该游戏的修改器，可自选版本下载")
        self._btn_dl.setFocusPolicy(Qt.NoFocus)
        self._btn_dl.clicked.connect(
            lambda: self.downloadTrainerRequested.emit(self._gid))
        self._btn_add = QPushButton("➕ 添加", self)
        self._btn_add.setObjectName("detailBtn")
        self._btn_add.setToolTip("添加本机已有的修改器 exe")
        self._btn_add.setFocusPolicy(Qt.NoFocus)
        self._btn_add.clicked.connect(self._add_trainer)
        tr_head.addWidget(self._tr_title, 1)
        tr_head.addWidget(self._btn_dl)
        tr_head.addWidget(self._btn_add)
        lay.addLayout(tr_head)

        self._tr_list = QListWidget(self)
        self._tr_list.setObjectName("detailTrainers")
        self._tr_list.setSelectionMode(QAbstractItemView.NoSelection)
        self._tr_list.setFocusPolicy(Qt.NoFocus)
        self._tr_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._tr_list.verticalScrollBar().setSingleStep(12)
        lay.addWidget(self._tr_list, 1)

        # 底部：编辑 / 删除游戏
        foot = QHBoxLayout()
        self._btn_edit = QPushButton("✏️ 编辑游戏", self)
        self._btn_edit.setObjectName("detailBtn")
        self._btn_edit.setFocusPolicy(Qt.NoFocus)
        self._btn_edit.clicked.connect(
            lambda: self.editGameRequested.emit(self._gid))
        self._btn_del = QPushButton("🗑 删除游戏", self)
        self._btn_del.setObjectName("detailBtn")
        self._btn_del.setFocusPolicy(Qt.NoFocus)
        self._btn_del.clicked.connect(
            lambda: self.deleteGameRequested.emit(self._gid))
        foot.addWidget(self._btn_edit)
        foot.addWidget(self._btn_del)
        foot.addStretch(1)
        lay.addLayout(foot)

    # ------------------------------------------------------------ 展示
    def show_game(self, gid):
        """切换到某款游戏的详情（数据变化后重复调用即刷新）。"""
        self._gid = gid
        self.refresh()

    def refresh(self):
        """按当前 gid 重读库数据并整体重绘（游戏被删时自动隐藏面板）。"""
        game = self._current_game()
        if game is None:
            gid_was = self._gid
            self._gid = None
            if gid_was is not None:
                self.hide()
            return
        self._name.setText(game["name"])
        self._name.setToolTip(game["name"])
        self._badge.setVisible(game["id"] in self._running)
        self._fill_stats(game)
        self._fill_launch(game)
        self._refresh_trainers(game)
        self._render_cover(game)

    def refresh_if_showing(self, gid):
        """仅当正在展示该游戏时刷新（如启动游戏后游玩统计要即时更新）。"""
        if gid == self._gid:
            self.refresh()

    def retheme(self):
        """主题切换后整体重绘：行/徽章按新配色重建，占位图强制换新主题的。"""
        self._last_cover_gid = None    # 绕过封面守卫：占位图是旧主题画的
        self.refresh()

    def set_running(self, gid, running):
        """与卡片模型同步的运行状态；正在展示该游戏时立即更新徽章。"""
        if running:
            self._running.add(gid)
        else:
            self._running.discard(gid)
        if gid == self._gid:
            self._badge.setVisible(running)

    def on_cover(self, gid):
        """封面异步加载完成（CoverLoader.cover_ready）：正在展示则立即换图。
        先清除"同游戏且同宽度"守卫，否则回调会被守卫吞掉，面板一直停在占位图。"""
        if gid == self._gid:
            self._last_cover_gid = None
            game = self._current_game()
            if game is not None:
                self._render_cover(game)

    def _current_game(self):
        return self._library.get_game(self._gid) if self._gid else None

    def _fill_stats(self, game):
        lp = game.get("last_played") or ""
        pc = int(game.get("play_count") or 0)
        rel = _rel_time(lp) if lp else "从未"
        text = f"上次游玩 {rel}  ·  启动 {pc} 次"
        secs = int(game.get("play_seconds") or 0)
        if secs >= 60:
            hours = secs / 3600
            dur = f"{hours:.1f}小时" if secs >= 3600 else f"{secs // 60}分钟"
            text += f"  ·  时长 {dur}"
        self._stats.setText(text)
        self._stats.setToolTip(
            f"上次游玩：{lp.replace('T', ' ')}" if lp else "还没有游玩记录")

    def _fill_launch(self, game):
        launch = game.get("launch") or {}
        lt = launch.get("type")
        # tooltip 每个分支都要赋值：不重置的话，从本地程序游戏切到
        # Steam/Epic 游戏，会残留上一款游戏的启动路径提示
        if lt == "steam":
            info = f"Steam · AppID {game.get('steam_id') or launch.get('value')}"
            self._launch_info.setToolTip(str(launch.get("value") or ""))
        elif lt == "epic":
            info = "Epic Games"
            self._launch_info.setToolTip(str(launch.get("value") or ""))
        elif lt == "file":
            info = f"本地程序 · {Path(str(launch.get('value') or '')).name}"
            self._launch_info.setToolTip(str(launch.get("value") or ""))
        else:
            info = "未配置启动方式（点「编辑游戏」设置）"
            self._launch_info.setToolTip("")
        self._launch_info.setText(info)

    def _render_cover(self, game):
        """大封面：内存缓存 → 向加载器请求（磁盘/网络后台取）→ 占位图兜底。
        宽度随面板伸缩，等比缩放；同游戏且宽度没变时跳过（防拖动分隔条反复缩放）。"""
        gid = game["id"]
        w = max(120, self.width() - 28)
        if gid == self._last_cover_gid and self._last_cover_w \
                and abs(w - self._last_cover_w) < 2:
            return
        self._last_cover_gid = gid
        self._last_cover_w = w
        pix = self._covers.get(gid)
        if pix is None:
            # 内存没有：发起异步加载（磁盘命中会同步回调 on_cover 换图），
            # 期间先显示占位图，避免空白闪烁
            self._covers.request(gid, game.get("cover_url") or "",
                                 game.get("cover_file") or "")
            pix = self._covers.get(gid)
            if pix is None:
                pix = self._covers.placeholder
        scaled = pix.scaled(w, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._cover.setPixmap(scaled)
        self._cover.setFixedSize(scaled.size())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 只有宽度真的变化（≥2px，拖分隔条）才重缩放封面——
        # 高度变化或逐像素抖动不需要，否则拖动时每个事件都在做平滑缩放。
        # 注意与 _last_cover_w 统一用"封面宽"（面板宽 - 28 边距）比较，
        # 单位错配会让守卫恒不生效、每帧都做一次 SmoothTransformation
        if self._gid and abs(self.width() - 28 - self._last_cover_w) >= 2:
            self._last_cover_gid = None
            self._last_cover_w = 0
            game = self._current_game()
            if game is not None:
                self._render_cover(game)

    # ------------------------------------------------------------ 修改器列表
    def _refresh_trainers(self, game=None):
        if game is None:
            game = self._current_game()
        trainers = list(game.get("trainers") or []) if game else []
        self._tr_title.setText(f"修改器（{len(trainers)}）")
        # 内容签名比对：签名不变就不重建行控件。refresh 挂在 modelReset 上，
        # 搜索框每敲一个字都会触发；列表内容没变时全量销毁重建既闪烁又费 CPU
        sig = (self._gid, tuple(
            (t.get("id"), str(t.get("name") or ""), str(t.get("version") or ""),
             bool(t.get("downloaded")), bool(t.get("update_available")),
             str(t.get("exe_path") or ""), str(t.get("note") or ""))
            for t in trainers))
        if sig == self._tr_sig:
            return
        self._tr_sig = sig
        # Qt 的 clear() 不销毁 setItemWidget 设置的行控件（只隐藏）。
        # 不手动销毁会持续泄漏 QWidget/按钮。先取引用再 deleteLater，再清列表。
        stale = [self._tr_list.itemWidget(self._tr_list.item(i))
                 for i in range(self._tr_list.count())]
        self._tr_list.clear()
        for w in stale:
            if w is not None:
                w.deleteLater()
        if not trainers:
            hint = QListWidgetItem()
            lb = QLabel("还没有修改器——点右上「➕ 添加」，"
                        "或用工具栏「⬇️ 下载修改器」", self)
            lb.setObjectName("detailDim")
            lb.setWordWrap(True)
            lb.setContentsMargins(10, 8, 10, 8)
            hint.setSizeHint(QSize(0, 54))
            self._tr_list.addItem(hint)
            self._tr_list.setItemWidget(hint, lb)
            return
        for t in trainers:
            it = QListWidgetItem()
            row = self._build_trainer_row(t)
            it.setSizeHint(QSize(0, row.sizeHint().height()))
            self._tr_list.addItem(it)
            self._tr_list.setItemWidget(it, row)

    def _build_trainer_row(self, t):
        """一个修改器一行：名称占满行宽（长名不截断），meta+路径一行，
        底部 启动/更新/目录/移除 四个按钮横排。"""
        tid = t["id"]
        row = QWidget(self)
        row.setObjectName("trainerRow")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 7, 8, 7)
        v.setSpacing(3)

        # 名称直接用记录原文（下载入库的名字本身常带来源前缀，不再重复拼接）；
        # 来源统一展示在 meta 行
        lb_name = QLabel(str(t["name"]), row)
        lb_name.setStyleSheet("font-weight: bold; background: transparent;")
        lb_name.setToolTip(str(t["name"]))
        v.addWidget(lb_name)

        bits = []
        if t.get("version"):
            bits.append(f"v{t['version']}")
        bits.append("官网下载" if t.get("downloaded") else "本地添加")
        if t.get("update_available"):
            bits.append("⬆ 有新版")
        exe = str(t.get("exe_path") or "")
        short = str(Path(exe).parent.name + "\\" + Path(exe).name) if exe else ""
        if short:
            bits.append(short)
        lb_meta = QLabel(" · ".join(bits), row)
        lb_meta.setObjectName("detailDim")
        lb_meta.setToolTip(exe or str(t["name"]))
        v.addWidget(lb_meta)

        # 用户备注（快捷键 / 注意事项），有才显示
        note = str(t.get("note") or "")
        if note:
            lb_note = QLabel("备注：" + note.replace("\n", " "), row)
            lb_note.setObjectName("detailPath")
            lb_note.setToolTip(note)
            v.addWidget(lb_note)

        btns = QHBoxLayout()
        btns.setSpacing(4)

        def small(text, tip, cb):
            b = QPushButton(text, row)
            b.setObjectName("detailBtn")
            b.setFixedHeight(24)      # 宽度自适应文字：窄面板 5 个按钮也放得下
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda checked=False: cb())
            return b

        btns.addWidget(small("▶ 启动", "启动该修改器",
                             lambda: self.launchTrainerRequested.emit(self._gid, tid)))
        if t.get("downloaded"):
            btns.addWidget(small("↻ 更新", "检查官网新版并更新",
                                 lambda: self.checkTrainerUpdateRequested.emit(self._gid, tid)))
        btns.addWidget(small("📂 目录", "打开修改器所在文件夹",
                             lambda: self.openTrainerFolderRequested.emit(self._gid, tid)))
        btns.addWidget(small("✏ 备注", "记一条备注（快捷键 / 注意事项）",
                             lambda: self._edit_note(tid, str(t.get("note") or ""))))
        btns.addWidget(small("✕ 移除", "从库中移除该修改器（不删磁盘文件）",
                             lambda: self._remove_trainer(tid)))
        btns.addStretch(1)
        v.addLayout(btns)
        v.addSpacing(2)
        return row

    # ------------------------------------------------------------ 面板内操作
    def _edit_note(self, tid, current):
        """编辑修改器备注（快捷键 / 注意事项），保存回库并刷新本行。"""
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getMultiLineText(
            self, "修改器备注", "记录这只修改器的快捷键、注意事项等：", current)
        if not ok:
            return
        self._library.update_trainer(self._gid, tid, note=text.strip())
        self.trainersChanged.emit(self._gid)

    def _add_trainer(self):
        if not self._gid:
            return
        dlg = AddTrainerDialog(self._library, self._gid, self)
        if dlg.exec():
            self.trainersChanged.emit(self._gid)

    def _remove_trainer(self, tid):
        if QMessageBox.question(
                self, "移除修改器",
                "确定移除该修改器？（仅移除库记录，不删除磁盘文件）",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._library.remove_trainer(self._gid, tid)
            self.trainersChanged.emit(self._gid)

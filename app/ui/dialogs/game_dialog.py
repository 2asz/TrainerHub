"""添加游戏 / 编辑游戏对话框（含后台 Steam 搜索 worker）。

注意：两个对话框刻意保持两个类——启动方式种类、封面处理、保存逻辑差异
足够大，合并成"带 editing 标志的单类"会塞满 if 分支，比重复更难维护。"""
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ...theme import current as T
from ._common import _StyledDialog, save_local_cover


# ================================================================ 添加游戏
class _SteamSearchWorker(QThread):
    """后台搜索 Steam AppID（storesearch API），避免阻塞 UI。可取消。"""
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, query, parent=None):
        super().__init__(parent)
        self._query = query
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ...steam_import import search_steam_appid
        try:
            if self._stop.is_set():
                return
            self.done.emit(search_steam_appid(self._query, cancel=self._stop))
        except Exception as e:
            self.failed.emit(str(e))


class _SteamSearchMixin:
    """AddGameDialog / EditGameDialog 共用的 Steam 搜索交互。

    原先两个类各持一份约 95% 重复的副本（唯一实质差别是空结果提示文案），
    收敛到这里。宿主需具备属性：_search_worker（初始 None）、_search_btn、
    _steam_results、_steam_hint、_name；并自行实现 _on_pick（选中行为不同）。"""

    _NO_RESULT_MSG = "未找到匹配的 Steam 游戏，可尝试更精确的名称，或手动填写 AppID。"

    def _search(self):
        query = self._name.text().strip()
        if not query:
            QMessageBox.information(self, "提示",
                                    "先在「游戏名称」输入游戏名，再点搜索。")
            return
        if self._search_worker is not None and self._search_worker.isRunning():
            return
        self._search_btn.setEnabled(False)
        self._search_btn.setText("搜索中…")
        self._steam_hint.setText(f"正在搜索「{query}」…")
        self._steam_results.clear()
        self._steam_results.setVisible(False)
        worker = _SteamSearchWorker(query, self)
        worker.done.connect(self._on_search_done)
        worker.failed.connect(self._on_search_failed)
        # 结束即清引用（必须在 deleteLater 之前连）：不清的话第二次点搜索
        # 会对已销毁的 C++ 对象调 isRunning() 直接 RuntimeError，按钮永久失效
        worker.finished.connect(lambda w=worker: self._clear_search_worker(w))
        worker.finished.connect(worker.deleteLater)   # 线程真正结束后释放
        self._search_worker = worker
        worker.start()

    def _clear_search_worker(self, w):
        if self._search_worker is w:
            self._search_worker = None

    def _on_search_done(self, items):
        self._search_btn.setEnabled(True)
        self._search_btn.setText("🔍 按名称搜索")
        self._steam_results.clear()
        if not items:
            self._steam_hint.setText(self._NO_RESULT_MSG)
            self._steam_results.setVisible(False)
            return
        for it in items:
            self._steam_results.addItem(f"{it['name']}（AppID {it['appid']}）", it)
        self._steam_results.setVisible(True)
        self._steam_hint.setText(f"找到 {len(items)} 个结果，请选择。")

    def _on_search_failed(self, err):
        self._search_btn.setEnabled(True)
        self._search_btn.setText("🔍 按名称搜索")
        self._steam_hint.setText(f"搜索出错：{err}\n请检查网络后重试，或手动填写 AppID。")


class AddGameDialog(_SteamSearchMixin, _StyledDialog):
    def __init__(self, library, parent=None):
        super().__init__(parent, "添加游戏", 660, 400)
        self._library = library
        self._search_worker = None
        self._picked = None
        self._cover_picked = None      # 手动选择的封面本地路径（添加后生效）
        # 名称是否由程序自动填充（选 exe / 搜索选中）。True 时换 exe 会重新自动填；
        # 用户手动编辑过则置 False，之后不再覆盖用户输入。
        self._auto_name = False

        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("可自动填写：选 exe 自动提取，或搜索 AppID 自动填入")
        self._name.textEdited.connect(self._on_name_edited)
        form.addRow("游戏名称", self._name)

        self._launch_type = QComboBox()
        self._launch_type.addItem("Steam 游戏", "steam")
        self._launch_type.addItem("本地程序", "file")
        self._launch_type.currentIndexChanged.connect(self._on_type)
        form.addRow("启动方式", self._launch_type)

        # Steam 区：AppID + 按名称搜索
        self._steam_widget = QWidget()
        sv = QVBoxLayout(self._steam_widget)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(4)
        h1 = QHBoxLayout()
        self._steam_id = QLineEdit()
        self._steam_id.setPlaceholderText("AppID（如 281990）")
        self._search_btn = QPushButton("🔍 按名称搜索")
        self._search_btn.clicked.connect(self._search)
        h1.addWidget(self._steam_id, 1)
        h1.addWidget(self._search_btn)
        sv.addLayout(h1)
        self._steam_results = QComboBox()
        self._steam_results.setVisible(False)
        self._steam_results.currentIndexChanged.connect(self._on_pick)
        sv.addWidget(self._steam_results)
        self._steam_hint = QLabel("输入游戏名 → 点「按名称搜索」→ 选择结果即可自动填充。")
        self._steam_hint.setStyleSheet("color: %s; font-size: 12px;" % T()["text_dim"])
        self._steam_hint.setWordWrap(True)
        sv.addWidget(self._steam_hint)
        form.addRow("Steam AppID", self._steam_widget)

        # 本地区：exe 自动提取名称
        self._file_row = QWidget()
        h = QHBoxLayout(self._file_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._file = QLineEdit()
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        h.addWidget(self._file, 1)
        h.addWidget(btn)
        form.addRow("可执行文件", self._file_row)

        self._process = QLineEdit()
        self._process.setPlaceholderText("进程名，逗号分隔（选 exe 后自动填）")
        form.addRow("进程名", self._process)

        # 封面：手动选本地图片（可选，添加时即可设置）
        self._cover_btn = QPushButton("选择封面图片…")
        self._cover_btn.clicked.connect(self._pick_cover)
        self._cover_status = QLabel("可选：本地图片封面（不加也可，稍后编辑时设置）")
        self._cover_status.setStyleSheet("color: %s; font-size: 12px;" % T()["text_dim"])
        cover_row = QWidget()
        cr = QHBoxLayout(cover_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self._cover_btn)
        cr.addWidget(self._cover_status, 1)
        form.addRow("封面", cover_row)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("添加")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addStretch(1)
        lay.addWidget(box)
        self._on_type()

    def _pick_cover(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if p:
            self._cover_picked = p
            self._cover_status.setText(f"已选：{Path(p).name}（添加后生效）")

    def _on_type(self):
        is_steam = self._launch_type.currentData() == "steam"
        self._steam_widget.setVisible(is_steam)
        self._file_row.setVisible(not is_steam)
        self._process.setVisible(not is_steam)

    # ---------- exe 自动填充 ----------
    def _on_name_edited(self, text):
        """用户手动输入名称后，停止自动覆盖。"""
        self._auto_name = False

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择程序", "", "程序 (*.exe)")
        if p:
            self._fill_from_exe(p)

    def _fill_from_exe(self, p):
        """按新选中的 exe 刷新名称/进程名。
        仅当名称尚未被用户手动编辑过（_auto_name 为 True 或为空）时重新自动填充，
        避免出现"换了个 exe 名称还是上一个"的情况。"""
        self._file.setText(p)
        self._process.setText(Path(p).name)
        if self._auto_name or not self._name.text().strip():
            self._auto_name = True
            from ...exeinfo import get_exe_info
            info = get_exe_info(p)
            if info:
                name = (info.get("product_name") or "").strip() or \
                       (info.get("file_description") or "").strip()
                if name:
                    self._name.setText(name)
                    return
            # 无元数据时用所在文件夹名（游戏目录名通常即游戏名）
            self._name.setText(Path(p).parent.name)

    # ---------- Steam 按名称搜索（交互逻辑见 _SteamSearchMixin） ----------
    def _on_pick(self, idx):
        it = self._steam_results.itemData(idx)
        if it is None:
            return
        self._steam_id.setText(it["appid"])
        if not self._name.text().strip():
            self._name.setText(it["name"])
            self._auto_name = True
        self._picked = it

    def _accept(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入游戏名称。")
            return
        if self._launch_type.currentData() == "steam":
            sid = self._steam_id.text().strip()
            if not sid.isdigit():
                QMessageBox.warning(self, "提示",
                                    "Steam AppID 需为数字。\n可用「按名称搜索」自动获取。")
                return
            launch = {"type": "steam", "value": sid}
            process_names = []
            cover_url = (self._picked or {}).get("cover_url")
            game, created = self._library.add_game(name, steam_id=sid,
                                                   launch=launch,
                                                   cover_url=cover_url)
            if not created:
                QMessageBox.information(
                    self, "已存在",
                    f"「{game['name']}」已在库中，本次操作合并到已有记录。")
        else:
            exe = self._file.text().strip()
            if not exe:
                QMessageBox.warning(self, "提示", "请选择可执行文件。")
                return
            launch = {"type": "file", "value": exe}
            process_names = [p.strip() for p in self._process.text().split(",") if p.strip()]
            if not process_names and exe.lower().endswith(".exe"):
                process_names = [Path(exe).name]
            game, created = self._library.add_game(name, launch=launch,
                                                   process_names=process_names)
            if not created:
                QMessageBox.information(
                    self, "已存在",
                    f"「{game['name']}」已在库中，本次操作合并到已有记录。")
        # 手动选择的本地封面：复制进应用数据目录并清掉 cover_url——
        # 封面加载器优先用网络封面，不清的话手选封面会被后来的网络封面覆盖
        if getattr(self, "_cover_picked", None) and game:
            local = save_local_cover(game["id"], self._cover_picked)
            if local:
                self._library.update_game(game["id"], cover_file=local,
                                          cover_url=None)
        # 未手选封面时不在这里生成：提取 exe 图标可能全盘扫游戏目录
        # （秒级到数十秒），统一由主窗口的 _schedule_cover_fetch 后台补齐
        self.accept()


# ================================================================ 编辑游戏
class EditGameDialog(_SteamSearchMixin, _StyledDialog):
    def __init__(self, library, gid, parent=None):
        super().__init__(parent, "编辑游戏", 660, 380)
        self._library = library
        self._gid = gid
        self._search_worker = None
        self._cover_picked = None      # 手动选择的封面本地路径（保存时生效）
        self._auto_name = False        # 与 AddGameDialog 对齐：_on_pick 里的判据
        game = library.get_game(gid)

        form = QFormLayout()
        self._name = QLineEdit(game["name"])
        form.addRow("游戏名称", self._name)

        launch = game.get("launch") or {}
        self._launch_type = QComboBox()
        self._launch_type.addItem("Steam 游戏", "steam")
        self._launch_type.addItem("Epic 游戏", "epic")
        self._launch_type.addItem("本地程序", "file")
        kind = launch.get("type")
        idx = {"steam": 0, "epic": 1, "file": 2}.get(kind, 2)
        self._launch_type.setCurrentIndex(idx)
        self._launch_type.currentIndexChanged.connect(self._on_type)
        form.addRow("启动方式", self._launch_type)

        # Steam 区：AppID + 按名称搜索
        self._steam_widget = QWidget()
        sv = QVBoxLayout(self._steam_widget)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(4)
        h1 = QHBoxLayout()
        self._steam_id = QLineEdit(str(game.get("steam_id") or ""))
        self._steam_id.setPlaceholderText("AppID（如 281990）")
        self._search_btn = QPushButton("🔍 按名称搜索")
        self._search_btn.clicked.connect(self._search)
        h1.addWidget(self._steam_id, 1)
        h1.addWidget(self._search_btn)
        sv.addLayout(h1)
        self._steam_results = QComboBox()
        self._steam_results.setVisible(False)
        self._steam_results.currentIndexChanged.connect(self._on_pick)
        sv.addWidget(self._steam_results)
        self._steam_hint = QLabel("搜索后选择结果可自动填充 AppID。")
        self._steam_hint.setStyleSheet("color: %s; font-size: 12px;" % T()["text_dim"])
        self._steam_hint.setWordWrap(True)
        sv.addWidget(self._steam_hint)
        form.addRow("Steam AppID", self._steam_widget)

        self._epic_row = QWidget()
        eh = QHBoxLayout(self._epic_row)
        eh.setContentsMargins(0, 0, 0, 0)
        self._epic_value = QLineEdit(
            launch.get("value", "") if kind == "epic" else "")
        self._epic_value.setPlaceholderText("com.epicgames.launcher://…")
        eh.addWidget(self._epic_value, 1)
        form.addRow("启动协议", self._epic_row)

        self._file_row = QWidget()
        h = QHBoxLayout(self._file_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._file = QLineEdit(launch.get("value", "") if launch.get("type") == "file" else "")
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        h.addWidget(self._file, 1)
        h.addWidget(btn)
        form.addRow("可执行文件", self._file_row)

        self._process = QLineEdit(", ".join(game.get("process_names", [])))
        form.addRow("进程名（逗号分隔）", self._process)

        # 本地程序启动参数（.lnk 导入时保留，编辑后不丢失）
        self._args_row = QWidget()
        ah = QHBoxLayout(self._args_row)
        ah.setContentsMargins(0, 0, 0, 0)
        self._args = QLineEdit(launch.get("args", "") if launch.get("type") == "file" else "")
        self._args.setPlaceholderText("启动参数（可选）")
        ah.addWidget(self._args, 1)
        form.addRow("启动参数", self._args_row)

        # 封面：手动选本地图片（复制进应用数据目录，离线可靠）
        self._cover_btn = QPushButton("选择封面图片…")
        self._cover_btn.clicked.connect(self._pick_cover)
        self._cover_status = QLabel(self._cover_state_text(game))
        self._cover_status.setStyleSheet("color: %s; font-size: 12px;" % T()["text_dim"])
        cover_row = QWidget()
        cr = QHBoxLayout(cover_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self._cover_btn)
        cr.addWidget(self._cover_status, 1)
        form.addRow("封面", cover_row)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addStretch(1)
        lay.addWidget(box)
        self._on_type()

    @staticmethod
    def _cover_state_text(game) -> str:
        if game.get("cover_file"):
            return "当前：本地图片 ✓"
        if game.get("cover_url"):
            return "当前：网络封面（保存后可用本地图片替换）"
        return "未设置（仅 Steam/Epic 会自动拉取）"

    def _pick_cover(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if p:
            self._cover_picked = p
            self._cover_status.setText(f"已选：{Path(p).name}（保存后生效）")

    def _on_type(self):
        kind = self._launch_type.currentData()
        self._steam_widget.setVisible(kind == "steam")
        self._epic_row.setVisible(kind == "epic")
        self._file_row.setVisible(kind == "file")
        self._process.setVisible(kind == "file")
        self._args_row.setVisible(kind == "file")

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择程序", "", "程序 (*.exe)")
        if p:
            self._file.setText(p)

    # ---------- Steam 按名称搜索（交互逻辑见 _SteamSearchMixin） ----------
    def _on_pick(self, idx):
        it = self._steam_results.itemData(idx)
        if it is None:
            return
        self._steam_id.setText(it["appid"])
        # 只在名称还是空/自动填充时覆盖，不冲掉用户手动改过的名字
        if not self._name.text().strip() or self._auto_name:
            self._name.setText(it["name"])

    def _accept(self):
        if getattr(self, "_accepting", False):
            return          # 封面异步解码进行中：OK 连点会重复提交（双份改库）
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入游戏名称。")
            return
        kind = self._launch_type.currentData()
        if kind == "steam":
            sid = self._steam_id.text().strip()
            if not sid.isdigit():
                QMessageBox.warning(self, "提示", "Steam AppID 需为数字。")
                return
            launch = {"type": "steam", "value": sid}
            steam_id = sid
        elif kind == "epic":
            value = self._epic_value.text().strip()
            if not value:
                QMessageBox.warning(self, "提示", "请输入 Epic 启动协议（com.epicgames.launcher://…）。")
                return
            launch = {"type": "epic", "value": value}
            steam_id = None
        else:
            exe = self._file.text().strip()
            if not exe:
                QMessageBox.warning(self, "提示", "请选择可执行文件。")
                return
            launch = {"type": "file", "value": exe}
            args = self._args.text().strip()
            if args:
                launch["args"] = args
            steam_id = None

        fields = {"name": name}
        if kind == "file":
            process_names = [p.strip() for p in self._process.text().split(",") if p.strip()]
            if not process_names and exe.lower().endswith(".exe"):
                process_names = [Path(exe).name]
            fields["process_names"] = process_names
        else:
            # 切换为 Steam/Epic：旧进程名不再有效。
            # 若不清理，旧 exe 仍会判定游戏"正在运行"，进而错误自动启动修改器
            fields["process_names"] = []
        # 手动选择的封面：先在后台验证图片可解码（读大图不卡 UI）。
        # 注意落盘必须延后到回调里——启动方式变更会重建游戏 id，封面文件
        # 名按 gid 哈希命名，旧 gid 落盘会与游戏当前 id 不一致。
        # 封面解析期间用户可能点取消——「改启动方式」也一并延后到回调里
        # 按取消与否提交或丢弃，保证「取消 = 不产生任何变化」
        if getattr(self, "_cover_picked", None):
            self._cover_btn.setEnabled(False)
            self._cover_status.setText("正在处理封面…")
            self._accepting = True

            class _CoverDecodeWorker(QThread):
                """后台验证手选图片可解码（不落盘，落盘在拿到新 gid 之后）。"""
                done = Signal(bool)             # 图片是否可解析

                def __init__(self, src, parent=None):
                    super().__init__(parent)
                    self._src = src

                def run(self):
                    from PySide6.QtGui import QImage
                    img = QImage(self._src)
                    self.done.emit(not img.isNull())

            w = _CoverDecodeWorker(self._cover_picked, self)
            w.done.connect(self._on_cover_checked)
            w.finished.connect(w.deleteLater)
            self._cover_worker = w
            self._pending_launch = launch
            self._pending_steam_id = steam_id
            self._pending_fields = fields
            w.start()
            return
        game, err = self._library.change_launch_target(self._gid, launch, steam_id)
        if game is None:
            QMessageBox.warning(self, "无法修改", err)
            return
        self._library.update_game(game["id"], **fields)
        self.accept()

    def _on_cover_checked(self, ok):
        self._accepting = False       # 防连点守卫复位（取消/失败/成功都要）
        # 用户在封面处理期间点了取消：丢弃本次全部修改（含启动方式变更），
        # 保证「取消 = 不产生任何变化」。
        # 注意不能用 result() 判取消——QDialog 未 close 前 result() 恒为
        # Rejected(0)，用它会让正常路径永远提前 return（改动被静默丢弃）
        if self._cancelled:
            return
        self._cover_btn.setEnabled(True)
        game, err = self._library.change_launch_target(
            self._gid, getattr(self, "_pending_launch", None),
            getattr(self, "_pending_steam_id", None))
        if game is None:
            QMessageBox.warning(self, "无法修改", err)
            self.reject()
            return
        fields = getattr(self, "_pending_fields", None) or {}
        if ok:
            # 落盘在拿到新 gid 之后：文件名按当前游戏 id 哈希（manual- 前缀），
            # 与网络封面缓存彻底分开；同时清掉 cover_url，防止被官方封面覆盖
            local = save_local_cover(game["id"], self._cover_picked)
            if local:
                fields["cover_file"] = local
                fields["cover_url"] = None
                self._cover_status.setText("封面已更新。")
            else:
                self._cover_status.setText("所选图片无法解析，已忽略（封面保持原样）。")
        else:
            self._cover_status.setText("所选图片无法解析，已忽略（封面保持原样）。")
        self._library.update_game(game["id"], **fields)
        self.accept()

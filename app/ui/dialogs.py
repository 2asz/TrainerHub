"""对话框：添加/编辑游戏、管理修改器、下载、扫描结果、设置。

新手提示：所有对话框统一继承 _StyledDialog，样式集中写在它的 setStyleSheet()
（QSS，类似 CSS）。QSS 里的 :hover（鼠标悬停）/ :pressed（鼠标按下）就是
"点击反馈"的来源——按下时颜色加深、内容下沉 1px；
objectName="primary" 的按钮是主操作按钮（蓝色高亮，见 primary_btn()）。"""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QPushButton, QRadioButton,
                               QVBoxLayout, QWidget)

from .. import audit
from ..config import config, SOURCES, DATA_DIR
from ..security import sha256_file, sanitize_component, unique_component
from ..downloader.base import DownloadError
from ..launcher import launch_trainer, open_folder
from ..covers import generate_cover_for_game

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def trainer_dest_dir(game, library, source) -> Path:
    """该游戏修改器的落盘目录（重复下载必须复用原目录，不再另开新夹）：
    1) 游戏已有修改器目录（trainer.dir_path 有效）→ 直接复用；
    2) trainers/<来源>/<游戏目录名> 已存在且未被**其他游戏**占用 → 复用
       （此前误用 unique_component 把"已存在"当冲突，重复下载会建出
        "游戏名 (2)"、"游戏名 (3)" 越积越多）；
    3) 只有目录名被其他游戏占用（真同名冲突）时才加后缀避让。"""
    base_dir = config.trainers_root / source
    base_dir.mkdir(parents=True, exist_ok=True)
    # 1) 复用该游戏已有修改器目录
    for t in game.get("trainers", []):
        dp = t.get("dir_path")
        if dp and Path(dp).is_dir():
            return Path(dp)
    # 其他游戏已占用的目录名（含其磁盘目录）
    claimed = set()
    for g in library.all_games():
        if g["id"] == game["id"]:
            continue
        for t in g.get("trainers", []):
            d = t.get("dir_path")
            if d:
                claimed.add(Path(d).name.casefold())
    name = game_dir_name(game["name"])
    if name.casefold() in claimed:
        existing = {d.name for d in base_dir.iterdir()}
        return base_dir / unique_component(name, existing | claimed)
    return base_dir / name          # 存在则复用，不存在则由安装流程创建


def game_dir_name(name: str) -> str:
    """按 naming_language 生成下载目录名：
    zh=中文原名（净化）；en=拼音/英文（拼音下划线连接）。"""
    if config.get("naming_language") == "en":
        try:
            from pypinyin import lazy_pinyin
            parts = lazy_pinyin(name or "")
            if parts:
                return sanitize_component("_".join(parts))
        except Exception:
            pass
    return sanitize_component(name)


def save_local_cover(gid, src_path) -> str | None:
    """手动选封面：读取本地图片 → 统一转 PNG → 复制进应用数据目录
    data/covers/<sha256(gid)>.png（与网络封面缓存同路径，显示链路零改动）。
    参考 potatoVN：封面复制进应用数据目录，避免依赖用户原始文件。
    返回复制后绝对路径；解码失败返回 None。"""
    import hashlib
    from PySide6.QtGui import QImage
    covers = DATA_DIR / "covers"
    try:
        covers.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    img = QImage(str(src_path))
    if img.isNull():
        return None
    dst = covers / (hashlib.sha256(str(gid).encode("utf-8")).hexdigest()[:16] + ".png")
    try:
        if img.save(str(dst), "PNG"):
            return str(dst)
    except OSError:
        pass
    return None


class _StyledDialog(QDialog):
    """统一样式基底。"""

    def __init__(self, parent=None, title="", w=560, h=420):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(w, h)
        self.setStyleSheet("""
            QDialog { background: #14171d; color: #e8ecf2; font-size: 13px;
                      font-family: "Microsoft YaHei UI"; }
            QLabel { color: #cfd5de; }
            QLineEdit, QComboBox, QListWidget, QPlainTextEdit {
                background: #1a1f28; border: 1px solid #262d3a; border-radius: 6px;
                padding: 6px 9px; selection-background-color: #3d8bfd; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #3d8bfd; }
            QComboBox::drop-down { border: none; width: 22px; }
            QComboBox QAbstractItemView { background: #1b1f27; border: 1px solid #262d3a;
                                          selection-background-color: #2c6fd6; }
            QListWidget { background: #171a21; border: 1px solid #262d3a; }
            QListWidget::item { padding: 5px 8px; border-radius: 5px; color: #cfd5de; }
            QListWidget::item:selected { background: #2c6fd6; color: white; }
            QListWidget::item:hover { background: #1f2630; }
            QPushButton { background: #222a35; border: 1px solid #2e3745;
                          border-radius: 6px; padding: 6px 15px; color: #e8ecf2; }
            QPushButton:hover { background: #2b3543; border-color: #3d8bfd; }
            QPushButton:pressed { background: #1c232d; padding: 7px 15px 5px 15px; }
            QPushButton:disabled { color: #6a7280; background: #1c2027; }
            QPushButton#primary { background: #3d8bfd; border: none; color: white;
                                  font-weight: bold; }
            QPushButton#primary:hover { background: #5aa0ff; }
            QPushButton#primary:pressed { background: #2c6fd6;
                                          padding: 7px 15px 5px 15px; }
            QGroupBox { border: 1px solid #262d3a; border-radius: 8px; margin-top: 8px;
                        padding-top: 10px; background: #161a21; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                               color: #9aa3b2; }
            QProgressBar { background: #1a1f28; border: none; border-radius: 5px;
                           height: 14px; text-align: center; color: #e8ecf2; }
            QProgressBar::chunk { background: #3d8bfd; border-radius: 5px; }
            QRadioButton, QCheckBox { color: #cfd5de; spacing: 6px; }
            QScrollBar:vertical { background: transparent; width: 10px; }
            QScrollBar::handle:vertical { background: #343c48; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #454f5e; }
            QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
            QMessageBox { background: #14171d; }
            QMessageBox QLabel { color: #e8ecf2; }
        """)

    def primary_btn(self, text):
        b = QPushButton(text)
        b.setObjectName("primary")
        return b

    def closeEvent(self, e):
        """关闭前停止并等待所有后台子线程（带超时）。
        超时未退出时拒绝关闭（e.ignore()）——网络请求有内置超时，
        避免运行中的 QThread 随父对象销毁导致 'Destroyed while thread is still running' 崩溃。"""
        from PySide6.QtCore import QThread
        import time
        threads = self.findChildren(QThread)
        for t in threads:
            if hasattr(t, "request_cancel"):
                t.request_cancel()
        deadline = time.monotonic() + 10
        for t in threads:
            while t.isRunning() and time.monotonic() < deadline:
                t.wait(50)
        if any(t.isRunning() for t in threads):
            e.ignore()      # 后台任务仍未退出，本次拒绝关闭（稍后可再关）
            return
        super().closeEvent(e)


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
        from ..steam_import import search_steam_appid
        try:
            if self._stop.is_set():
                return
            self.done.emit(search_steam_appid(self._query, cancel=self._stop))
        except Exception as e:
            self.failed.emit(str(e))


class AddGameDialog(_StyledDialog):
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
        self._steam_hint.setStyleSheet("color: #9aa3b2; font-size: 12px;")
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
        self._cover_status.setStyleSheet("color: #9aa3b2; font-size: 12px;")
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
            from ..exeinfo import get_exe_info
            info = get_exe_info(p)
            if info:
                name = (info.get("product_name") or "").strip() or \
                       (info.get("file_description") or "").strip()
                if name:
                    self._name.setText(name)
                    return
            # 无元数据时用所在文件夹名（游戏目录名通常即游戏名）
            self._name.setText(Path(p).parent.name)

    # ---------- Steam 按名称搜索 ----------
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
        worker.finished.connect(worker.deleteLater)   # 结束后释放，防线程对象积累
        self._search_worker = worker
        worker.start()

    def _on_search_done(self, items):
        self._search_btn.setEnabled(True)
        self._search_btn.setText("🔍 按名称搜索")
        self._steam_results.clear()
        if not items:
            self._steam_hint.setText(
                "未找到匹配的 Steam 游戏，可尝试更精确的名称，或手动填写 AppID。")
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
            game, _ = self._library.add_game(name, steam_id=sid, launch=launch,
                                             cover_url=cover_url)
        else:
            exe = self._file.text().strip()
            if not exe:
                QMessageBox.warning(self, "提示", "请选择可执行文件。")
                return
            launch = {"type": "file", "value": exe}
            process_names = [p.strip() for p in self._process.text().split(",") if p.strip()]
            if not process_names and exe.lower().endswith(".exe"):
                process_names = [exe.rsplit("\\", 1)[-1]]
            game, _ = self._library.add_game(name, launch=launch, process_names=process_names)
        # 手动选择的本地封面：入库后复制进应用数据目录（后台处理）
        if getattr(self, "_cover_picked", None) and game:
            local = save_local_cover(game["id"], self._cover_picked)
            if local:
                self._library.update_game(game["id"], cover_file=local)
        elif game and not (game.get("cover_url") or game.get("cover_file")):
            # 用户未手选封面：统一生成离线封面（本地/Steam/Epic 都支持）
            cover = generate_cover_for_game(game)
            if cover:
                self._library.update_game(game["id"], cover_file=cover)
        self.accept()


# ================================================================ 编辑游戏
class EditGameDialog(_StyledDialog):
    def __init__(self, library, gid, parent=None):
        super().__init__(parent, "编辑游戏", 660, 380)
        self._library = library
        self._gid = gid
        self._search_worker = None
        self._cover_picked = None      # 手动选择的封面本地路径（保存时生效）
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
        self._steam_hint.setStyleSheet("color: #9aa3b2; font-size: 12px;")
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
        self._cover_status.setStyleSheet("color: #9aa3b2; font-size: 12px;")
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
        self._steam_results.clear()
        self._steam_results.setVisible(False)
        worker = _SteamSearchWorker(query, self)
        worker.done.connect(self._on_search_done)
        worker.failed.connect(self._on_search_failed)
        worker.finished.connect(worker.deleteLater)   # 结束后释放，防线程对象积累
        self._search_worker = worker
        worker.start()

    def _on_search_done(self, items):
        self._search_btn.setEnabled(True)
        self._search_btn.setText("🔍 按名称搜索")
        self._steam_results.clear()
        if not items:
            self._steam_hint.setText("未找到匹配的 Steam 游戏，可尝试更精确的名称。")
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

    def _on_pick(self, idx):
        it = self._steam_results.itemData(idx)
        if it is None:
            return
        self._steam_id.setText(it["appid"])
        self._name.setText(it["name"])

    def _accept(self):
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

        # 统一经 change_launch_target 重建稳定 ID（Steam/Epic/本地 互相切换、
        # 本地路径变更都会重新计算 id 并做冲突检查，避免重复记录）
        game, err = self._library.change_launch_target(self._gid, launch, steam_id)
        if game is None:
            QMessageBox.warning(self, "无法修改", err)
            return
        fields = {"name": name}
        if kind == "file":
            process_names = [p.strip() for p in self._process.text().split(",") if p.strip()]
            if not process_names and exe.lower().endswith(".exe"):
                process_names = [exe.rsplit("\\", 1)[-1]]
            fields["process_names"] = process_names
        else:
            # 切换为 Steam/Epic：旧进程名不再有效。
            # 若不清理，旧 exe 仍会判定游戏"正在运行"，进而错误自动启动修改器
            fields["process_names"] = []
        # 手动选择的封面：后台复制进应用数据目录（大图解码不卡 UI），
        # 完成后更新 cover_file（替换网络封面）
        if getattr(self, "_cover_picked", None):
            self._cover_btn.setEnabled(False)
            self._cover_status.setText("正在处理封面…")

            class _CoverSaveWorker(QThread):
                """后台保存本地封面，完成后在主线程回调。"""
                done = Signal(str, object)      # gid, 本地封面路径或 None

                def __init__(self, gid, src, parent=None):
                    super().__init__(parent)
                    self._gid = gid
                    self._src = src

                def run(self):
                    self.done.emit(self._gid, save_local_cover(self._gid, self._src))

            w = _CoverSaveWorker(game["id"], self._cover_picked, self)
            w.done.connect(self._on_cover_saved)
            w.finished.connect(w.deleteLater)
            self._cover_worker = w
            w.start()
            self._pending_fields = fields
            self._pending_game_id = game["id"]
            return
        self._library.update_game(game["id"], **fields)
        self.accept()

    def _on_cover_saved(self, gid, local):
        self._cover_btn.setEnabled(True)
        fields = getattr(self, "_pending_fields", None)
        if local:
            fields["cover_file"] = local
            fields["cover_url"] = None
            self._library.update_game(gid, **fields)
            self.accept()
        else:
            self._cover_status.setText("所选图片无法解析，已忽略（封面保持原样）。")
            self._library.update_game(gid, **fields)
            self.accept()


# ================================================================ 管理修改器
class ManageTrainersDialog(_StyledDialog):
    def __init__(self, library, gid, parent=None):
        super().__init__(parent, "管理修改器", 720, 460)
        self._library = library
        self._gid = gid
        game = library.get_game(gid)

        title = QLabel(f"{game['name']} · 修改器")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")

        self._list = QListWidget()
        self._refresh()

        btn_add = QPushButton("添加修改器…")
        btn_add.clicked.connect(self._add)
        btn_start = QPushButton("启动")
        btn_start.setObjectName("primary")
        btn_start.clicked.connect(self._start)
        btn_folder = QPushButton("打开目录")
        btn_folder.clicked.connect(self._folder)
        btn_remove = QPushButton("移除")
        btn_remove.clicked.connect(self._remove)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        for b in (btn_add, btn_start, btn_folder, btn_remove, btn_close):
            row.addWidget(b)
        row.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(self._list, 1)
        lay.addLayout(row)

    def _refresh(self):
        self._list.clear()
        for t in self._library.trainers_of(self._gid):
            ver = f" v{t['version']}" if t.get("version") else ""
            dl = "· 官网下载" if t.get("downloaded") else ""
            it = QListWidgetItem(f"[{t['source']}] {t['name']}{ver}  {dl}")
            it.setData(Qt.UserRole, t["id"])
            it.setToolTip(str(t["exe_path"]))
            self._list.addItem(it)

    def _current(self):
        it = self._list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _add(self):
        dlg = AddTrainerDialog(self._library, self._gid, self)
        if dlg.exec():
            self._refresh()

    def _start(self):
        tid = self._current()
        if not tid:
            return
        trainer = next((t for t in self._library.trainers_of(self._gid)
                        if t["id"] == tid), None)
        if not trainer:
            return
        # 与主窗口一致的平衡型安全策略：官网下载的修改器首次运行需一键确认
        if trainer.get("downloaded") and not trainer.get("first_run_confirmed"):
            ret = QMessageBox.question(
                self, "安全确认",
                f"该修改器由本软件从官网自动下载。\n"
                f"SHA-256: {(trainer.get('sha256') or '未知')[:16]}…\n\n"
                f"首次运行需确认（确认后将直接启动，不再询问）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            self._library.update_trainer(self._gid, tid, first_run_confirmed=True)
        ok, msg = launch_trainer(trainer["exe_path"], as_admin=True)
        if not ok:
            QMessageBox.warning(self, "启动失败", msg)

    def _folder(self):
        tid = self._current()
        if not tid:
            return
        trainer = next((t for t in self._library.trainers_of(self._gid)
                        if t["id"] == tid), None)
        if trainer and trainer.get("dir_path"):
            open_folder(trainer["dir_path"])
        elif trainer:
            open_folder(Path(trainer["exe_path"]).parent)

    def _remove(self):
        tid = self._current()
        if not tid:
            return
        if QMessageBox.question(self, "移除修改器",
                                "确定移除该修改器？（仅移除库记录，不删除磁盘文件）",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._library.remove_trainer(self._gid, tid)
            self._refresh()


class AddTrainerDialog(_StyledDialog):
    """手动添加修改器：选择 exe + 来源 + 复制进库/保留路径。"""

    def __init__(self, library, gid, parent=None):
        super().__init__(parent, "添加修改器", 600, 400)
        self._library = library
        self._gid = gid
        game = library.get_game(gid)

        form = QFormLayout()
        self._name = QLineEdit(f"{game['name']} 修改器")
        form.addRow("名称", self._name)

        self._exe = QLineEdit()
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._exe, 1)
        h.addWidget(btn)
        form.addRow("可执行文件", w)

        self._source = QComboBox()
        for s in SOURCES:
            self._source.addItem(s, s)
        # 默认「本地」：手动添加/扫描的都是本机文件（下载器入库才会用「风灵月影」）
        idx = self._source.findData("本地")
        if idx >= 0:
            self._source.setCurrentIndex(idx)
        form.addRow("来源", self._source)

        self._version = QLineEdit()
        self._version.setPlaceholderText("如 2026.08.11（可选）")
        form.addRow("版本", self._version)

        self._copy_mode = QRadioButton("复制到修改器库目录（推荐）")
        self._keep_mode = QRadioButton("保留原路径（不复制）")
        self._copy_mode.setChecked(True)
        form.addRow("存放方式", self._copy_mode)
        form.addRow("", self._keep_mode)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("添加")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addStretch(1)
        lay.addWidget(box)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择修改器", "", "程序 (*.exe)")
        if p:
            self._exe.setText(p)
            if not self._name.text() or self._name.text().endswith("修改器"):
                self._name.setText(f"{Path(p).parent.name} 修改器")

    def _accept(self):
        exe = self._exe.text().strip()
        if not exe or not Path(exe).is_file():
            QMessageBox.warning(self, "提示", "请选择有效的修改器程序。")
            return
        name = self._name.text().strip() or Path(exe).name
        source = self._source.currentData()
        version = self._version.text().strip()
        game = self._library.get_game(self._gid)
        sha = sha256_file(exe)
        if self._copy_mode.isChecked():
            # 同一游戏重复添加复用原目录（仅他游戏占用同名时避让）
            dest = trainer_dest_dir(game, self._library, source)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / Path(exe).name
            if target.resolve() != Path(exe).resolve():
                import shutil
                shutil.copy2(exe, target)
            exe_path = str(target)
            dir_path = str(dest)
        else:
            exe_path = exe
            dir_path = str(Path(exe).parent)
        self._library.add_trainer(self._gid, source=source, name=name,
                                  exe_path=exe_path, dir_path=dir_path,
                                  version=version, sha256=sha, downloaded=False)
        self.accept()


# ================================================================ 扫描结果
class ScanResultDialog(_StyledDialog):
    """扫描结果：勾选候选 exe，分配游戏 + 来源后入库。"""

    def __init__(self, library, candidates, parent=None):
        super().__init__(parent, "扫描结果", 760, 520)
        self._library = library
        self._candidates = candidates

        self._list = QListWidget()
        for p in candidates:
            it = QListWidgetItem(str(p))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, p)
            self._list.addItem(it)

        form = QFormLayout()
        self._game_combo = QComboBox()
        self._game_combo.addItem("（新建游戏）", None)
        for g in library.all_games():
            self._game_combo.addItem(g["name"], g["id"])
        form.addRow("分配到游戏", self._game_combo)

        self._new_game = QLineEdit()
        self._new_game.setPlaceholderText("新建游戏名称")
        self._new_game.setEnabled(False)
        self._game_combo.currentIndexChanged.connect(
            lambda i: self._new_game.setEnabled(self._game_combo.itemData(i) is None))
        form.addRow("新游戏名", self._new_game)

        self._source = QComboBox()
        for s in SOURCES:
            self._source.addItem(s, s)
        # 默认「本地」：手动添加/扫描的都是本机文件（下载器入库才会用「风灵月影」）
        idx = self._source.findData("本地")
        if idx >= 0:
            self._source.setCurrentIndex(idx)
        form.addRow("来源", self._source)

        # 自动识别 trainers/<来源>/<游戏名>/ 结构并预选（放进去就能一键入库）
        self._auto_preselect()

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("入库")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"识别到 {len(candidates)} 个疑似修改器，勾选要入库的："))
        lay.addWidget(self._list, 1)
        lay.addLayout(form)
        lay.addWidget(box)

    # ---------- 自动预选 ----------
    def _auto_preselect(self):
        """若候选来自 trainers/<来源>/<游戏名>/ 目录，自动预选游戏与来源。
        库中有同名游戏则直接选中；否则预填"新建游戏"名称，用户确认即可。"""
        import re
        m = None
        for p in self._candidates:
            m = re.search(r"[\\/]trainers[\\/]([^\\/]+)[\\/]([^\\/]+)[\\/]", str(p))
            if m:
                break
        if not m:
            return
        source, game_name = m.group(1), m.group(2).strip()

        # 来源预选
        idx = self._source.findData(source)
        if idx >= 0:
            self._source.setCurrentIndex(idx)

        # 游戏预选：先精确后模糊匹配库内游戏
        target = None
        for g in self._library.all_games():
            if g["name"] == game_name:
                target = g
                break
        if target is None:
            for g in self._library.all_games():
                if game_name.lower() in g["name"].lower() or g["name"].lower() in game_name.lower():
                    target = g
                    break
        if target is not None:
            idx = self._game_combo.findData(target["id"])
            if idx >= 0:
                self._game_combo.setCurrentIndex(idx)
        else:
            self._new_game.setText(game_name)
            self._new_game.setEnabled(True)

    def _accept(self):
        picked = []
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it.checkState() == Qt.Checked:
                picked.append(it.data(Qt.UserRole))
        if not picked:
            self.reject()
            return
        game_id = self._game_combo.currentData()
        if game_id is None:
            name = self._new_game.text().strip()
            if not name:
                QMessageBox.warning(self, "提示", "请填写新游戏名称。")
                return
            game, _ = self._library.add_game(name)
            game_id = game["id"]
        source = self._source.currentData()
        for p in picked:
            sha = sha256_file(p)
            self._library.add_trainer(game_id, source=source,
                                      name=f"{Path(p).name} 修改器",
                                      exe_path=p, dir_path=str(Path(p).parent),
                                      sha256=sha, downloaded=False)
        self.accept()


# ================================================================ 下载
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

    def _on_progress(self, done, total):
        self.progress.emit(done, total)


class DownloadDialog(_StyledDialog):
    """下载修改器（风灵月影源）。失败时可降级为复制下载链接。"""

    def __init__(self, library, game_id, adapter, downloader, pool, parent=None):
        super().__init__(parent, "下载修改器", 720, 560)
        self._library = library
        self._adapter = adapter
        self._downloader = downloader
        self._pool = pool
        self._worker = None
        self._last_url = None
        # 并发下载限制（download_concurrency 实际生效）：同时只允许 N 个 install
        self._dl_sem = threading.BoundedSemaphore(
            max(1, int(config.get("download_concurrency"))))

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
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(QLabel("游戏："))
        top.addWidget(self._game_combo, 1)
        top.addWidget(self._search_btn)

        row = QHBoxLayout()
        row.addWidget(self._dl_btn)
        row.addWidget(self._copy_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self._status)
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
        if not self._dl_sem.acquire(blocking=False):
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
            self._dl_sem.release()      # 创建失败也要释放名额
            raise

    def _start_download(self, game, page_url, entry=None):
        # 重复下载复用原目录（同一游戏不再新建 "名字 (2)"）
        dest = trainer_dest_dir(game, self._library, self._adapter.SOURCE)
        ver = (entry or {}).get("version", "")
        self._set_busy(True, f"正在下载{' v' + ver if ver else ''}…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._worker = _DownloadWorker(
            self._adapter, self._downloader, "install",
            page_url=page_url, game_name=game["name"], dest_root=dest,
            entry=entry, parent=self)
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
        self._set_busy(False, "")
        self._progress.setVisible(False)
        self._dl_sem.release()      # 释放并发下载名额
        gid = self._game_combo.currentData()
        game = self._library.get_game(gid)
        safe_game = sanitize_component(game["name"])
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
        self._set_busy(False, f"下载失败：{err}")
        self._progress.setVisible(False)
        self._dl_sem.release()      # 释放并发下载名额

    def _copy_url(self):
        if self._last_url:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._last_url)
            self._status.setText("链接已复制到剪贴板。")

    def _set_busy(self, busy, text):
        self._status.setText(text)
        self._search_btn.setEnabled(not busy)
        self._dl_btn.setEnabled(not busy and bool(self._results.selectedItems()))
        self._game_combo.setEnabled(not busy)


# ================================================================ 设置
class SettingsDialog(_StyledDialog):
    def __init__(self, parent=None):
        super().__init__(parent, "设置", 620, 480)
        form = QFormLayout()

        self._root = QLineEdit(str(config.trainers_root))
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse_root)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._root, 1)
        h.addWidget(btn)
        form.addRow("修改器库目录", w)

        self._naming = QComboBox()
        self._naming.addItem("中文目录名", "zh")
        self._naming.addItem("英文/拼音目录名", "en")
        self._naming.setCurrentIndex(0 if config.get("naming_language") == "zh" else 1)
        form.addRow("目录命名", self._naming)

        self._poll = QLineEdit(str(config.get("poll_interval_ms")))
        form.addRow("进程检测间隔(ms)", self._poll)

        self._auto = QCheckBox("游戏运行时自动启动对应修改器（默认关闭）")
        self._auto.setChecked(bool(config.get("auto_start_trainer")))
        form.addRow("", self._auto)

        group = QGroupBox("Windows Defender 白名单")
        gv = QVBoxLayout(group)
        gl = QLabel("修改器可能被杀毒软件误报。可将修改器库目录加入 Defender 排除项。")
        gl.setWordWrap(True)
        gbtn = QPushButton("一键加入排除项")
        gbtn.clicked.connect(self._defender)
        gv.addWidget(gl)
        gv.addWidget(gbtn)
        form.addRow(group)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addStretch(1)
        lay.addWidget(box)

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择修改器库目录")
        if d:
            self._root.setText(d)

    def _defender(self):
        import subprocess
        root = Path(self._root.text().strip() or str(config.trainers_root))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if QMessageBox.question(self, "确认", f"将以下目录加入 Defender 排除项？\n{root}",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            safe_root = str(root).replace("'", "''")
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Add-MpPreference -ExclusionPath '{safe_root}'"],
                           capture_output=True, timeout=60)
            QMessageBox.information(self, "完成", "已提交白名单操作，若被系统拦截请手动确认。")
        except Exception as e:
            QMessageBox.warning(self, "失败", f"操作失败：{e}")

    def _accept(self):
        try:
            poll = int(self._poll.text().strip())
            if not (200 <= poll <= 60000):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "提示", "进程检测间隔需为 200-60000 的整数。")
            return
        config.set("trainers_root", self._root.text().strip())
        config.set("naming_language", self._naming.currentData())
        config.set("poll_interval_ms", poll)
        config.set("auto_start_trainer", self._auto.isChecked())
        self.accept()

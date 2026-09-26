"""扫描结果入库对话框 + 后台校验 worker。"""
import re
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialogButtonBox, QFormLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressDialog, QVBoxLayout)

from ... import audit
from ...config import SOURCES
from ...security import sha256_file
from ._common import _StyledDialog


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
        if getattr(self, "_accepting", False):
            return          # 后台校验进行中：OK 连点会起多个 worker 重复入库
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
        # sha256 要读完整个文件（修改器包常几百 MB），必须放后台线程，
        # 否则多选时界面长时间冻结
        dlg = QProgressDialog("正在计算校验值并入库…", "取消", 0, len(picked), self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()
        w = _ScanAcceptWorker(self._library, game_id, source, picked, self)
        w.progress.connect(dlg.setValue)
        w.done.connect(lambda ok, fail: self._on_accept_done(dlg, ok, fail))
        w.finished.connect(w.deleteLater)   # 线程真正结束后释放
        dlg.canceled.connect(w.request_cancel)
        self._accept_worker = w
        self._accepting = True
        w.start()

    def _on_accept_done(self, dlg, ok, fail):
        dlg.close()
        self._accepting = False
        if fail:
            QMessageBox.warning(
                self, "部分入库失败",
                f"成功 {ok} 个，失败 {fail} 个（文件可能被移动/占用），"
                "详情见 data\\audit.log。")
        self.accept()


class _ScanAcceptWorker(QThread):
    """后台完成扫描结果入库：逐个计算 sha256（大文件耗时）并写入库。可取消。"""
    progress = Signal(int, int)     # 已完成, 总数
    done = Signal(int, int)         # 成功, 失败

    def __init__(self, library, game_id, source, items, parent=None):
        super().__init__(parent)
        self._library = library
        self._gid = game_id
        self._source = source
        self._items = items             # [exe 路径]
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        ok = fail = 0
        total = len(self._items)
        for i, p in enumerate(self._items):
            if self._stop.is_set():
                break
            try:
                sha = sha256_file(p)
                # 取消可能发生在耗时数秒的 sha256 期间：算完复查一次，
                # 否则已取消的批次仍会把当前条目入库且无任何提示
                #（2026-09-09 审查 P3-2，违背"取消=不产生变化"契约）
                if self._stop.is_set():
                    break
                self._library.add_trainer(
                    self._gid, source=self._source,
                    name=f"{Path(p).name} 修改器",
                    exe_path=p, dir_path=str(Path(p).parent),
                    sha256=sha, downloaded=False)
                ok += 1
            except OSError as e:
                audit.warning(f"扫描入库失败 {p}: {e}")
                fail += 1
            self.progress.emit(i + 1, total)
        self.done.emit(ok, fail)

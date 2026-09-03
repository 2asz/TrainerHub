"""手动添加修改器对话框 + 后台复制/校验 worker。"""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLineEdit,
                               QMessageBox, QProgressDialog, QPushButton,
                               QRadioButton, QVBoxLayout, QWidget)

from ...config import SOURCES
from ...security import sha256_file
from ._common import _StyledDialog, trainer_dest_dir


# ================================================================ 管理修改器
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
        if getattr(self, "_accepting", False):
            return          # 后台校验进行中：OK 连点会起多个 worker 重复入库
        exe = self._exe.text().strip()
        if not exe or not Path(exe).is_file():
            QMessageBox.warning(self, "提示", "请选择有效的修改器程序。")
            return
        name = self._name.text().strip() or Path(exe).name
        source = self._source.currentData()
        version = self._version.text().strip()
        game = self._library.get_game(self._gid)
        if not game:
            QMessageBox.warning(self, "提示", "该游戏已不存在。")
            return
        copy_mode = self._copy_mode.isChecked()
        try:
            # sha256 要读完整个文件（修改器常几百 MB）+ 复制也耗时：
            # 放后台线程跑，界面不冻结；done 回调里在主线程入库
            dest = trainer_dest_dir(game, self._library, source) if copy_mode else None
            if dest is not None:
                dest.mkdir(parents=True, exist_ok=True)
        except (RuntimeError, OSError) as e:
            QMessageBox.warning(self, "无法添加", str(e))
            return
        dlg = QProgressDialog("正在计算校验值并入库…", "取消", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()
        w = _TrainerAddWorker(exe, str(dest) if dest else "", copy_mode, self)
        w.done.connect(lambda ok, msg: self._on_add_done(dlg, w, ok, msg,
                                                         name, source, version))
        dlg.canceled.connect(w.request_cancel)
        self._add_worker = w
        self._accepting = True
        w.start()

    def _on_add_done(self, dlg, w, ok, msg, name, source, version):
        dlg.close()
        self._accepting = False
        if not ok:
            if msg == "已取消" or self._cancelled:
                return          # 用户主动取消：不算失败，不弹"添加失败"
            QMessageBox.warning(self, "添加失败", msg)
            return
        exe_path, dir_path, sha = msg.split("\n", 2)
        self._library.add_trainer(self._gid, source=source, name=name,
                                  exe_path=exe_path, dir_path=dir_path or None,
                                  version=version, sha256=sha, downloaded=False)
        self.accept()


class _TrainerAddWorker(QThread):
    """后台完成添加修改器的重活：复制文件（可选）+ 计算 SHA-256。可取消。"""
    done = Signal(bool, str)        # ok, "exe_path\ndir_path\nsha256" 或错误消息

    def __init__(self, exe, dest, copy_mode, parent=None):
        super().__init__(parent)
        self._exe = exe
        self._dest = dest
        self._copy_mode = copy_mode
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        import shutil
        try:
            src = Path(self._exe)
            if self._copy_mode:
                dest = Path(self._dest)
                target = dest / src.name
                if target.resolve() != src.resolve():
                    if self._stop.is_set():
                        self.done.emit(False, "已取消")
                        return
                    shutil.copy2(src, target)
                exe_path, dir_path = str(target), str(dest)
            else:
                exe_path, dir_path = str(src), str(src.parent)
            if self._stop.is_set():
                self.done.emit(False, "已取消")
                return
            sha = sha256_file(exe_path)
            self.done.emit(True, f"{exe_path}\n{dir_path}\n{sha}")
        except OSError as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")

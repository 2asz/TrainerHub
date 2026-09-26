"""设置对话框：常规配置 + Windows Defender 白名单 + 游戏库导出/导入。"""
import re
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressDialog,
                               QPushButton, QVBoxLayout, QWidget)

from ...config import config, DATA_DIR
from ...library import LIBRARY_PATH
from ...theme import dialog_qss, set_theme as theme_set, theme_name
from ._common import _StyledDialog


class _ImportApplyWorker(QThread):
    """后台完成导入落盘：备份当前数据 → 原子替换 → 重载内存。

    核心逻辑在 Library.import_replace 里（全程持库锁）——磁盘覆盖与内存
    load 之间绝不能插入主线程的防抖保存，否则旧内存写回磁盘会把导入
    静默回滚（2026-09-02 审查 P1-4）。失败时先 load() 对齐内存与磁盘再上报。"""
    done = Signal(bool, str)      # ok, err

    def __init__(self, library, staged, parent=None):
        super().__init__(parent)
        self._library = library
        self._staged = Path(staged)

    def run(self):
        try:
            self._library.import_replace(self._staged / "library.json",
                                         self._staged / "covers")
            self.done.emit(True, "")
        except Exception as e:
            try:
                self._library.load()   # 失败也对齐内存与磁盘，防后续保存分叉
            except Exception:
                pass
            self.done.emit(False, f"{type(e).__name__}: {e}")
        finally:
            # 暂存清理放 worker 自己身上：被孤儿接管断开 done 信号时
            # （导入中关闭对话框），_done 永不执行也不能泄漏 512MB 暂存
            import shutil
            shutil.rmtree(self._staged, ignore_errors=True)


# ================================================================ 设置
class SettingsDialog(_StyledDialog):
    """设置对话框。主题切换即时生效（取消时还原），其余设置保存后生效。"""

    def __init__(self, library, parent=None, theme_changed=None):
        super().__init__(parent, "设置", 620, 500)
        self._library = library                  # 导出/导入游戏库用
        self._theme_changed = theme_changed      # 主窗口回调：切换后重刷样式
        self._theme_prev = theme_name()          # 打开时的主题（取消则还原）
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

        # 数据目录只读展示：只读环境回落 %LOCALAPPDATA% 后用户无从得知数据在哪
        data_hint = QLabel(str(DATA_DIR))
        data_hint.setObjectName("detailDim")
        data_hint.setWordWrap(True)
        data_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("数据目录", data_hint)

        # 主题：深色/浅色。切换即时生效（含当前对话框），点"取消"还原
        from ...theme import LABELS
        self._theme = QComboBox()
        for key, label in LABELS.items():
            self._theme.addItem(label, key)
        self._theme.setCurrentIndex(
            list(LABELS.keys()).index(theme_name()))
        self._theme.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("界面主题", self._theme)

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

        # 库备份：导出（游戏库 + 封面）为 zip，换机/重装时导入恢复
        group2 = QGroupBox("游戏库备份")
        gv2 = QVBoxLayout(group2)
        gl2 = QLabel("导出游戏库与全部封面为一个 zip（不含修改器 exe 本体）；\n"
                     "导入会覆盖当前游戏库——导入前会自动备份现有数据。")
        gl2.setWordWrap(True)
        brow = QHBoxLayout()
        b_exp = QPushButton("导出游戏库…")
        b_exp.clicked.connect(self._export_library)
        b_imp = QPushButton("导入游戏库…")
        b_imp.clicked.connect(self._import_library)
        brow.addWidget(b_exp)
        brow.addWidget(b_imp)
        brow.addStretch(1)
        gv2.addWidget(gl2)
        gv2.addLayout(brow)
        form.addRow(group2)

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

    def _on_theme_changed(self, _idx):
        """主题下拉变化：立即切换（主界面 + 当前对话框），取消时还原。"""
        theme_set(self._theme.currentData())
        self.setStyleSheet(dialog_qss())
        if self._theme_changed:
            self._theme_changed()

    def reject(self):
        """取消设置：把主题还原成打开前的样子，并丢弃未应用的导入暂存
        （当前数据不动——导入只有点了「保存」才生效）。"""
        self._discard_pending_import()
        theme_set(self._theme_prev)
        self.setStyleSheet(dialog_qss())
        if self._theme_changed:
            self._theme_changed()
        super().reject()

    def _discard_pending_import(self):
        staged = getattr(self, "_pending_import", None)
        if staged is not None:
            self._pending_import = None
            import shutil
            shutil.rmtree(staged, ignore_errors=True)

    def _defender(self):
        from ...defender import defender_add_exclusion
        root = Path(self._root.text().strip() or str(config.trainers_root))
        if QMessageBox.question(self, "确认", f"将以下目录加入 Defender 排除项？\n{root}",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        ok, err = defender_add_exclusion(root)
        if ok:
            QMessageBox.information(self, "完成", "已提交白名单操作，若被系统拦截请手动确认。")
        else:
            # returncode 校验与主窗口共用同一实现：非管理员下不再静默假成功
            QMessageBox.warning(self, "失败",
                                "添加白名单未成功（可能需要以管理员身份运行本程序）。\n"
                                f"{err[:200]}")

    # ---- 游戏库导出 / 导入 ----
    def _export_library(self):
        """把 library.json + 全部封面打包为一个 zip（不含修改器 exe 本体）。"""
        import zipfile
        if getattr(self, "_importing", False):
            return          # 导入落盘进行中，拒绝并发导出
        default = f"trainerhub-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出游戏库", default, "备份包 (*.zip)")
        if not path:
            return
        try:
            lib_saved = self._library.save(force=True)   # 先落盘最新数据
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                if LIBRARY_PATH.exists():
                    zf.write(LIBRARY_PATH, "library.json")
                covers = DATA_DIR / "covers"
                if covers.is_dir():
                    for f in sorted(covers.iterdir()):
                        if f.is_file():
                            zf.write(f, f"covers/{f.name}")
            if not lib_saved:
                QMessageBox.warning(self, "导出", "库落盘时出现问题，导出的可能不是最新数据。")
            QMessageBox.information(self, "导出完成",
                                    f"已导出到：\n{path}\n\n"
                                    "包含游戏库与封面（不含修改器 exe 本体）。")
        except (OSError, zipfile.BadZipFile) as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _import_library(self):
        """从导出的 zip 恢复游戏库与封面（第一步：解包校验到暂存目录）。
        真正覆盖发生在点「保存」后（_apply_import → Library.import_replace，
        后台原子执行并自动备份）——此前解包覆盖直接生效，随后点「取消」
        也无法回退。"""
        import shutil
        import zipfile
        if getattr(self, "_importing", False):
            return          # 导入落盘进行中，拒绝并发导入/导出
        path, _ = QFileDialog.getOpenFileName(
            self, "导入游戏库", "", "备份包 (*.zip)")
        if not path:
            return
        if QMessageBox.question(
                self, "确认导入",
                "导入会覆盖当前的游戏库与封面（导入前自动备份现有数据）。\n"
                "点「保存」前不会动现有数据。\n继续解包？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            staged = None
            staged = Path(tempfile.mkdtemp(prefix="trainerhub-import-"))
            with zipfile.ZipFile(path) as zf:
                # 白名单 + 上限校验：备份包只可能包含我们导出的两种内容；
                # 裸 zf.extract 无总量上限（covers/ 前缀 zip 炸弹可撑满 %TEMP%）
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if len(infos) > 5000:
                    raise ValueError("备份包条目数超过上限（5000）")
                if sum(i.file_size for i in infos) > 512 * 1024 * 1024:
                    raise ValueError("备份包解压总量超过上限（512MB）")
                if not any(i.filename.replace("\\", "/") == "library.json"
                           for i in infos):
                    raise ValueError("备份包里没有 library.json，不是有效的游戏库备份")
                for i in infos:
                    name = i.filename.replace("\\", "/")
                    if name != "library.json" and not name.startswith("covers/"):
                        raise ValueError(f"拒绝备份包中的意外条目: {i.filename!r}")
                    if name.startswith("covers/") and "/" in name[len("covers/"):]:
                        # import_replace 逐文件平铺拷贝，嵌套子目录会半成功
                        raise ValueError(f"拒绝嵌套条目: {i.filename!r}")
                    if name.startswith("/") or re.match(r"^[a-zA-Z]:", name) \
                            or ".." in name.split("/") or ":" in name:
                        raise ValueError(f"拒绝非法备份条目: {i.filename!r}")
                    base = name.rsplit("/", 1)[-1].split(".")[0].upper()
                    if base in {"CON", "PRN", "AUX", "NUL"} \
                            or re.match(r"^(COM|LPT)[1-9]$", base):
                        raise ValueError(f"拒绝设备名条目: {i.filename!r}")
                zf.extract("library.json", staged)
                (staged / "covers").mkdir(parents=True, exist_ok=True)
                for i in infos:
                    name = i.filename.replace("\\", "/")
                    if name.startswith("covers/"):
                        zf.extract(i, staged)
                # 内容校验：library.json 必须可解析且 games 是 dict
                import json
                raw = json.loads((staged / "library.json").read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("games"), dict):
                    raise ValueError("备份包里的 library.json 不是有效的游戏库数据")
        except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
            QMessageBox.warning(self, "导入失败", str(e))
            return
        self._discard_pending_import()     # 丢弃旧的未应用暂存
        self._pending_import = staged
        QMessageBox.information(
            self, "已就绪",
            "备份包校验通过。\n点击「保存」完成导入；点「取消」放弃本次导入。")

    def _apply_import(self, staged):
        """第二步：后台原子导入（Library.import_replace，持库锁防保存竞态），
        完成后重载库并刷新主窗口封面。"""
        w = _ImportApplyWorker(self._library, staged, self)
        # 导入中途不可取消：半途而废比等几秒更糟（有自动备份兜底）
        dlg = QProgressDialog("正在导入游戏库…", "", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setCancelButton(None)
        dlg.show()
        self._importing = True

        def _keep_open():
            # ESC 会触发 QProgressDialog 的 reject：导入没完就把它拉回来，
            # 防止进度框消失后用户再点导入形成并发 worker
            if getattr(self, "_importing", False):
                dlg.show()

        dlg.rejected.connect(_keep_open)

        def _done(ok, err):
            self._importing = False
            dlg.close()
            import shutil
            shutil.rmtree(staged, ignore_errors=True)   # 暂存无论成败都清理
            if not ok:
                QMessageBox.warning(self, "导入失败", err)
                return          # 停留在设置页（worker 已 load 对齐内存与磁盘）
            try:
                from PySide6.QtWidgets import QApplication
                from ..main_window import MainWindow
                for win in QApplication.topLevelWidgets():
                    if isinstance(win, MainWindow):
                        win.refresh_covers()
                        break
            except Exception:
                pass
            QMessageBox.information(self, "导入完成",
                                    "游戏库与封面已恢复。\n如个别封面未刷新，重启应用即可。")
            self.accept()

        w.done.connect(_done)
        w.finished.connect(w.deleteLater)
        self._import_worker = w   # 保活引用：防 worker 被提前 GC
        w.start()

    def _accept(self):
        if getattr(self, "_importing", False):
            return          # 导入落盘进行中：禁止并发保存/导入
        try:
            poll = int(self._poll.text().strip())
            if not (200 <= poll <= 60000):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "提示", "进程检测间隔需为 200-60000 的整数。")
            return
        root_text = self._root.text().strip()
        if not root_text:
            # 空串写进配置会让 Path("") 变成"当前工作目录"，
            # 后续所有修改器都会落错位置——拒绝保存
            QMessageBox.warning(self, "提示", "修改器库目录不能为空。")
            return
        config.set("trainers_root", root_text)
        config.set("naming_language", self._naming.currentData())
        config.set("theme", self._theme.currentData())
        config.set("poll_interval_ms", poll)
        config.set("auto_start_trainer", self._auto.isChecked())
        # 有未应用的导入暂存 → 后台落盘，完成后再 accept()
        staged = getattr(self, "_pending_import", None)
        if staged is not None:
            self._pending_import = None
            self._apply_import(staged)
            return
        self.accept()

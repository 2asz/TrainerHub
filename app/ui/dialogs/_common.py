"""对话框公共件：_StyledDialog 样式基类 + 三个跨对话框复用的 helper。

trainer_dest_dir / game_dir_name 被 trainer_add、trainer_download 使用
（tasks.py 也经 dialogs 门面导入 trainer_dest_dir）；save_local_cover 被
game_dialog（添加/编辑游戏）使用；_StyledDialog 是全部 6 个对话框的基类。"""
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QDialog, QPushButton

from ...config import config, DATA_DIR
from ...security import sanitize_component, unique_component
from ...theme import dialog_qss


def trainer_dest_dir(game, library, source) -> Path:
    """该游戏修改器的落盘目录（重复下载必须复用原目录，不再另开新夹）：
    1) 游戏已有修改器目录（trainer.dir_path 有效）→ 直接复用；
    2) trainers/<来源>/<游戏目录名> 已存在且未被**其他游戏**占用 → 复用
       （此前误用 unique_component 把"已存在"当冲突，重复下载会建出
        "游戏名 (2)"、"游戏名 (3)" 越积越多）；
    3) 只有目录名被其他游戏占用（真同名冲突）时才加后缀避让。
    目录不可写（绿色版解压进 Program Files 等受保护目录）时抛 RuntimeError，
    调用方转成可行动的用户提示，而不是让 PermissionError 崩掉点击路径。"""
    base_dir = config.trainers_root / source
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"修改器目录不可写：{base_dir}\n"
            "请在「设置」里更换修改器库目录，或把程序解压到普通文件夹后重试。") from e
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
    data/covers/manual-<sha256(gid)>.png。

    文件名必须与网络封面缓存（<sha256(gid)>.png，无前缀）分开：
    手选封面是用户明确选择的，优先级最高、绝不能被官方封面下载覆盖；
    命名分开后，加载器按 manual- 前缀识别并优先使用（见 _CoverTask.run）。
    参考 potatoVN：封面复制进应用数据目录，避免依赖用户原始文件。
    返回复制后绝对路径；解码失败返回 None。"""
    from PySide6.QtGui import QImage
    from ..cover_loader import cover_hash
    covers = DATA_DIR / "covers"
    try:
        covers.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    img = QImage(str(src_path))
    if img.isNull():
        return None
    dst = covers / ("manual-" + cover_hash(gid) + ".png")
    try:
        if img.save(str(dst), "PNG"):
            return str(dst)
    except OSError:
        pass
    return None


class _StyledDialog(QDialog):
    """统一样式基底（颜色随主题：app/theme.py 的深色/浅色调色板）。"""

    def __init__(self, parent=None, title="", w=560, h=420):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(w, h)
        self.setStyleSheet(dialog_qss())
        # 显式取消标志：QDialog.result() 未 close 前恒为 Rejected（0），
        # 不能用它判断"用户点了取消"——异步回调里判取消必须用这个标志
        self._cancelled = False

    def reject(self):
        self._cancelled = True
        super().reject()

    def primary_btn(self, text):
        b = QPushButton(text)
        b.setObjectName("primary")
        return b

    def done(self, r):
        """accept/reject/close 的统一出口（Qt 只有 close() 走 closeEvent，
        点「取消/关闭」按钮走的是 reject/accept——取消逻辑放这里才全覆盖）：
        请求取消所有后台子线程，仍在运行的移交孤儿接管，对话框立即关闭。"""
        from ..main_window import _adopt_orphan_thread
        threads = self.findChildren(QThread)
        for t in threads:
            if hasattr(t, "request_cancel"):
                t.request_cancel()
        for t in threads:
            if t.isRunning():
                # 断开父子关系后对话框可安全销毁；线程结束自发 deleteLater
                _adopt_orphan_thread(t)
        super().done(r)

    def closeEvent(self, e):
        """标题栏 X：走 reject()，取消逻辑在 done() 里统一执行。"""
        self.reject()

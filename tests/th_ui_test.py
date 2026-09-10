"""UI 层回归（入库 tests/）：封面快照截胡端到端 + 主窗口冒烟。

直接运行本文件即可（脚本自设 sys.path；QT_QPA_PLATFORM=offscreen 自动设置）。
全程 TRAINERHUB_DATA_DIR 隔离，不触碰真实数据。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="th_ui_test_"))
os.environ["TRAINERHUB_DATA_DIR"] = str(_TMP)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILED = []


def check(name, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{tag}] {name} {extra}", flush=True)


from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.library import Library
from app.ui.card_view import GameListModel

lib = Library()
game, _ = lib.add_game("GameX", launch={"type": "file", "value": "C:/g/x.exe"})
gid = game["id"]
model = GameListModel(lib)
check("U1 入库 + reload 一行", model.rowCount() == 1)
check("U2 初始无封面路径",
      not (model.index(0, 0).data(GameListModel.Role_CoverFile) or ""))

# P1 端到端：库更新封面 → cover_updated → 模型必须立即反映新值
# （修复前 self._games[i] 挂旧快照对象，重绘读到旧路径，卡片一直占位图）
NEW_COVER = str(_TMP / "covers" / "new.png")
lib.update_game(gid, cover_file=NEW_COVER, cover_url=None)
model.cover_updated(gid)
val = model.index(0, 0).data(GameListModel.Role_CoverFile)
check("U3 P1 cover_updated 后模型与库一致", val == NEW_COVER, f"got={val!r}")
check("U4 game_at 行对象同步", model.game_at(0)["cover_file"] == NEW_COVER)

# cover_url 路径同样走刷新
lib.update_game(gid, cover_url="https://example/c.png", cover_file=None)
model.cover_updated(gid)
check("U5 cover_url 更新同步",
      model.index(0, 0).data(GameListModel.Role_CoverUrl) == "https://example/c.png"
      and not (model.index(0, 0).data(GameListModel.Role_CoverFile) or ""))

# 未知 gid 安全（无此行时不崩）
model.cover_updated("no-such-gid")
model.play_stats_changed("no-such-gid")
check("U6 未知 gid 安全", True)

# ---------- 游玩统计同步（2026-09-10 深度审查 P2-1）----------
# 修复前 touch_played/add_play_seconds 后只刷详情面板，卡片模型仍是旧快照：
# 游玩角标陈旧、「最近游玩」分类不重排
lib.touch_played(gid)
model.play_stats_changed(gid)
check("U7 游玩次数同步（play_count）",
      model.index(0, 0).data(GameListModel.Role_PlayCount) == 1,
      f"got={model.index(0, 0).data(GameListModel.Role_PlayCount)}")
check("U7b 上次游玩时间同步",
      bool(model.index(0, 0).data(GameListModel.Role_LastPlayed)))
lib.add_play_seconds(gid, 60)
model.play_stats_changed(gid)
check("U8 游玩时长同步（play_seconds）",
      model.index(0, 0).data(GameListModel.Role_PlaySeconds) == 60)
# 「最近游玩」分类按 last_played 过滤+倒序：该分支必须重排
model.set_source("最近游玩")
check("U9 最近游玩分类含刚启动的游戏", model.rowCount() == 1)
model.set_source("全部")

# ---------- 离线封面原子写（2026-09-10 深度审查 P2-2）----------
from app.covers import make_initial_cover, _cover_path_for_gid
from PySide6.QtGui import QImage

_cov = make_initial_cover(gid, "GameX")
check("U10 首字母封面生成成功", bool(_cov))
if _cov:
    _img = QImage(_cov)
    check("U10b 封面文件可解码（未截断）", not _img.isNull())
    check("U10c 无 .tmp 临时文件残留",
          not _cover_path_for_gid(gid).with_suffix(".tmp").exists())

# ---------- 主窗口冒烟（种子数据，隔离目录）----------
from app.config import DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
# 种子 exe 必须真实存在：启动清理（_StartupCleanWorker）会把启动目标
# 已不存在的本地游戏正当移除（虚构路径会让种子库被清空，V3 无数据可用）
exe1 = _TMP / "fake_a.exe"
exe1.touch()
exe2 = _TMP / "fake_b.exe"
exe2.touch()
lib2 = Library()
lib2.add_game("SmokeA", launch={"type": "file", "value": str(exe1)})
lib2.add_game("SmokeB", launch={"type": "file", "value": str(exe2)})
lib2.save(force=True)

from app.ui.main_window import MainWindow

w = MainWindow(lib2)
w.show()
app.processEvents()
import time as _t
_t.sleep(0.6)              # 等启动清理后台跑完（种子目标真实存在，无清理发生）
app.processEvents()
check("V1 主窗口构造 + 模型行数", w._model.rowCount() == 2,
      f"got={w._model.rowCount()}")
check("V2 标题含版本号", "1.3.1" in w.windowTitle(), w.windowTitle())
# P1 修复在主窗口链路上生效：用 lib2 自己的游戏（此前误用 lib 的 gid，
# update_game 无操作、断言恒真，2026-09-10 审查第三节-1）
g2 = lib2.all_games()[0]
g2_id = g2["id"]
lib2.update_game(g2_id, cover_file=NEW_COVER)
w._model.cover_updated(g2_id)
app.processEvents()
row = w._model._row_by_gid.get(g2_id)
v3_ok = row is not None and \
    w._model.index(row, 0).data(GameListModel.Role_CoverFile) == NEW_COVER
check("V3 主窗口链路 cover_updated 生效", v3_ok)

# ---------- 搜索框可用性：常见窗口宽度下必须可见 + Ctrl+F 可聚焦 ----------
# 修复前搜索框在工具栏末尾，1280（默认）/1440/1600 下被 Qt 折叠进扩展区 →
# isVisible()=False，而不可见控件无法获得键盘焦点，Ctrl+F 一并失效
#（2026-09-10 UI 审查 P1：默认尺寸下搜索功能完全不可用）。
# 现已移到侧边栏顶部，摆脱工具栏宽度约束——本组断言防止再次回归。
SIZES = (1024, 1280, 1366, 1600, 1920)
for _w in SIZES:
    w.resize(_w, 800)
    app.processEvents()                 # 等布局重算，否则读到旧几何
    _sb = w._search_box
    _vis = _sb.isVisible()
    _sb.clearFocus()
    w.setFocus()
    app.processEvents()
    QTest.keyClick(w, Qt.Key_F, Qt.ControlModifier)
    app.processEvents()
    _focus = _sb.hasFocus()
    check(f"V5 搜索框 {_w}px 可见且 Ctrl+F 可聚焦", _vis and _focus,
          f"visible={_vis} focus={_focus}")

w.close()
app.processEvents()
check("V4 关窗收尾无异常", True)

print("DONE", "FAILED=" + ",".join(FAILED) if FAILED else "ALL_OK", flush=True)
app.quit()
# 清理隔离目录：mkdtemp 不会自动回收，此前漏了这一步，每跑一次就残留在
# %TEMP% 一个目录（2026-09-10 发现累积 16 个）。与 th_lib_test 保持一致。
try:
    import logging
    logging.shutdown()
except Exception:
    pass
_t.sleep(0.2)          # 让 Qt 释放封面/缓存的文件句柄后再删
shutil.rmtree(_TMP, ignore_errors=True)
print("CLEANUP_OK", flush=True)
sys.exit(1 if FAILED else 0)

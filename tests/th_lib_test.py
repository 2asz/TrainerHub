"""数据安全回归（入库 tests/）。直接运行本文件即可（脚本自设 sys.path）。"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="th_lib_test_"))
os.environ["TRAINERHUB_DATA_DIR"] = str(_TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILED = []


def check(name, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{tag}] {name} {extra}", flush=True)


from app.library import Library
from app.config import DATA_DIR

LIBRARY_PATH = DATA_DIR / "library.json"

lib = Library()
lib.add_game("GameA", launch={"type": "file", "value": "C:/g/a.exe"})
check("T1 add_game", len(lib.games) == 1)

gid_a = lib.all_games()[0]["id"]
snap = lib.get_game(gid_a)
snap["name"] = "HACKED"
check("T2 快照深拷贝契约", lib.get_game(gid_a)["name"] == "GameA")

lib.update_game(gid_a, name="   ")
check("T3 空名被守门丢弃", lib.get_game(gid_a)["name"] == "GameA")

lib.update_game(gid_a, launch="C:/bad")
check("T4 launch 非 dict 被拒",
      lib.get_game(gid_a)["launch"] == {"type": "file", "value": "C:/g/a.exe"})

lib.update_game(gid_a, play_count="abc")
check("T5 play_count 非数字被拒",
      int(lib.get_game(gid_a).get("play_count") or 0) == 0)

lib.save(force=True)
with open(LIBRARY_PATH, "r", encoding="utf-8") as fh:
    data = json.load(fh)
check("T6 保存落盘", len(data["games"]) == 1)

# ---------- 损坏恢复链路 ----------
LIBRARY_PATH.write_text("{corrupted", encoding="utf-8")
lib2 = Library()
check("T7 损坏留证", bool(list(DATA_DIR.glob("library.corrupt-*.json"))))
check("T7b 无备份时空库+留痕说明",
      len(lib2.games) == 0 and bool(lib2.last_error))

lib3 = Library()
lib3.add_game("GameC", launch={"type": "file", "value": "C:/g/c.exe"})
lib3.save(force=True)
lib3.save(force=True)      # 两次保存确保轮转出备份
LIBRARY_PATH.write_text("garbage", encoding="utf-8")
lib4 = Library()
check("T8 从备份恢复 GameC",
      any(g["name"] == "GameC" for g in lib4.all_games()))
with open(LIBRARY_PATH, "r", encoding="utf-8") as fh:
    recovered = json.load(fh)
check("T8b 恢复后立即落盘", "GameC" in json.dumps(recovered, ensure_ascii=False))

# ---------- 轮转上限 ----------
gid_c = lib4.all_games()[0]["id"]
for i in range(8):
    lib4.update_game(gid_c, name=f"N{i}")
    lib4.save(force=True)
n_backups = len(list(DATA_DIR.glob("library.backup-*.json")))
check("T9 轮转保留 ≤5", n_backups <= 5, f"got={n_backups}")

# ---------- revision 单调 ----------
rev0 = lib4.revision
lib4.load()
check("T10 load 后 revision+1 不归零", lib4.revision == rev0 + 1,
      f"{rev0} -> {lib4.revision}")

# ---------- 缺 games 键 ----------
LIBRARY_PATH.write_text('{"version": 1}', encoding="utf-8")
lib5 = Library()
check("T11 缺 games 键按空库",
      len(lib5.games) == 0 and lib5.last_error is None)

# ---------- 重复 id 留痕（2026-09-09 审查 P3 修复项）----------
# 触发条件：外层 key 非法（空串）的记录用内嵌 id 归一，与另一条的外层 key 相同
dup = {"games": {
    "": {"id": "file-x", "name": "One",
         "launch": {"type": "file", "value": "C:/1.exe"}},
    "file-x": {"id": "file-x", "name": "Two",
               "launch": {"type": "file", "value": "C:/2.exe"}}}}
LIBRARY_PATH.write_text(json.dumps(dup), encoding="utf-8")
lib6 = Library()
check("T12 重复 id 只留一条", len(lib6.games) == 1)
audit_path = DATA_DIR / "audit.log"
audit_text = ""
if audit_path.exists():
    with open(audit_path, "r", encoding="utf-8", errors="replace") as fh:
        audit_text = fh.read()
check("T12b 重复 id audit 留痕", "重复 id" in audit_text)

# ---------- import_replace 事务 ----------
lib6.add_game("Keep", launch={"type": "file", "value": "C:/g/K.exe"})
lib6.save(force=True)
staged = _TMP / "staged_imp"
staged.mkdir(exist_ok=True)
staged_lib = staged / "library.json"
staged_lib.write_text(json.dumps({"games": {
    "file-abc": {"id": "file-abc", "name": "Imported",
                 "launch": {"type": "file", "value": "C:/i/I.exe"}}}}),
    encoding="utf-8")
lib6.import_replace(staged_lib, None)
check("T13 import_replace 替换内存", "Imported" in
      [g["name"] for g in lib6.all_games()])
check("T13b import_replace 建了备份",
      bool(list(DATA_DIR.glob("import-backup-*"))))
# covers 拷贝失败 → 异常上抛；library.json 未替换 = 完全回滚（旧库原样）
staged_lib.write_text(
    json.dumps({"games": {"x": {"id": "x", "name": "Bad2"}}}),
    encoding="utf-8")
(staged / "covers").mkdir(exist_ok=True)
(staged / "covers" / "c.png").mkdir()   # 目录当源：copy2 抛 IsADirectoryError
raised = False
try:
    lib6.import_replace(staged_lib, staged / "covers")
except Exception:
    raised = True
check("T14 覆盖失败异常上抛", raised)
lib6.load()
check("T14b 失败后旧库完好（未替换）", all(g["name"] != "Bad2"
                                           for g in lib6.all_games()))
check("T14c 暂存目录已清理",
      not (DATA_DIR / "covers.import-tmp").exists())

# ---------- prune 语义（2026-09-09 审查 P3-7 重写后行为不变）----------
lib6.add_trainer("file-abc", source="本地", name="T1",
                 exe_path="C:/no/such/trainer.exe")
lib6.add_trainer("file-abc", source="本地", name="T2", exe_path="")
removed = lib6.prune_missing_trainers()
check("T16 失效记录被移除", removed == 1)
trs = lib6.trainers_of("file-abc")
check("T16b 空 exe 记录保留",
      len(trs) == 1 and trs[0]["exe_path"] == "")
check("T17 prune 幂等", lib6.prune_missing_trainers() == 0)

# ---------- 外部数据解析：ACF 主程序选择（2026-09-10 深度审查 P2-3）----------
from app.install_info import _pick_acf_launch_executable

ACF_MULTI = '''
"AppState"
{
    "name"    "Test Game"
    "launch"
    {
        "0"
        {
            "executable"    "Support/RedistCheck.exe"
            "type"          "none"
        }
        "1"
        {
            "executable"    "Game.exe"
            "type"          "default"
        }
    }
}
'''
check("T18 ACF 优先取 type=default 的主程序",
      _pick_acf_launch_executable(ACF_MULTI) == "Game.exe",
      f"got={_pick_acf_launch_executable(ACF_MULTI)!r}")
check("T18b 无 default 时退化为首个 exe",
      _pick_acf_launch_executable(
          '"executable" "Only.exe" "type" "none"') == "Only.exe")
check("T18c 无 .exe 条目返回 None",
      _pick_acf_launch_executable('"executable" "readme.txt"') is None)

print("DONE", "FAILED=" + ",".join(FAILED) if FAILED else "ALL_OK", flush=True)

try:
    import logging
    logging.shutdown()
except Exception:
    pass
time.sleep(0.2)
shutil.rmtree(_TMP, ignore_errors=True)
print("CLEANUP_OK", flush=True)
sys.exit(1 if FAILED else 0)

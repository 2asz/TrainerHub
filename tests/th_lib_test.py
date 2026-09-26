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
# trainers 落点也要隔离：trainer_dest_dir 用 config.trainers_root，
# 不设会落到项目真实 trainers/ 目录（2026-09-13 自测发现并修正）
os.environ["TRAINERHUB_TRAINERS_ROOT"] = str(_TMP / "trainers")
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

# ---------- 小幸源解析（2026-09-11 接入；mock 网络只测解析）----------
from app.downloader.xiaoxing import XiaoXingDownloader

HOME_HTML = '''
<html><body>
<article><h2 class="entry-title"><a href="https://www.xiaoxingjie.com/archives/39.html">赛博朋克2077 多功能修改器 V2.6.2</a></h2></article>
<article><h2 class="entry-title"><a href="https://www.xiaoxingjie.com/archives/56.html">霍格沃茨之遗 多功能修改器 V1.5.5</a></h2></article>
<aside><a href="https://www.xiaoxingjie.com/archives/1.html">侧边栏小部件链接 不应被采集</a></aside>
</body></html>
'''
# 站内搜索「战神」的结果页（官网实测：战神4 不在首页列表，仅站内搜索命中）
SEARCH_HIT_HTML = '''
<html><body>
<article><h2 class="entry-title"><a href="https://www.xiaoxingjie.com/archives/70.html">战神4 十一项修改器 V1.0.1</a></h2></article>
<aside><a href="https://www.xiaoxingjie.com/archives/39.html">赛博朋克2077 多功能修改器 V2.6.2</a></aside>
</body></html>
'''
SEARCH_EMPTY_HTML = '<html><body><p>没有找到结果</p></body></html>'
# 全量枚举翻页样本：第 2 页 1 条新 + 1 条与第 1 页重复（验证去重）
PAGED_2_HTML = '''
<html><body>
<article><h2 class="entry-title"><a href="https://www.xiaoxingjie.com/archives/70.html">战神4 十一项修改器 V1.0.1</a></h2></article>
<article><h2 class="entry-title"><a href="https://www.xiaoxingjie.com/archives/39.html">赛博朋克2077 多功能修改器 V2.6.2</a></h2></article>
</body></html>
'''
DETAIL_HTML = '''
<html><body><p>下载地址：</p>
<a href="https://pan.baidu.com/s/1abc">百度网盘</a>
<a href="https://www.mediafire.com/file/s7ylunqqvhl1pnd/Cyberpunk.2077.Trainer.V2.6.2-XiaoXing.zip/file">MediaFire</a>
</body></html>
'''
MF_HTML = '''
<html><body>
<a href="https://download225.mediafire.com/abc123/Cyberpunk.2077.Trainer.V2.6.2-XiaoXing.zip">download</a>
</body></html>
'''
xd = XiaoXingDownloader(None)


def _fake_fetch(url, timeout=20):
    import urllib.parse as _up
    if "?s=" in url:
        q = _up.unquote(url.split("?s=", 1)[1].split("&")[0])
        if q == "修改器":                     # 全量枚举（翻页）
            if "paged=2" in url:
                return PAGED_2_HTML
            if "paged=" in url:
                return SEARCH_EMPTY_HTML
            return HOME_HTML
        return SEARCH_HIT_HTML if "战神" in q else SEARCH_EMPTY_HTML
    if "mediafire.com/file/" in url:
        return MF_HTML
    if "/archives/" in url:
        return DETAIL_HTML
    return HOME_HTML


xd.fetch_page = _fake_fetch      # 实例覆盖：解析逻辑不触网
xd._fetch_via_curl = lambda url: MF_HTML   # MediaFire 页走 curl 路径，同样 mock
# 站内搜索命中，且侧边栏小部件链接被选择器排除（只 1 条）
res = xd.search("战神4")
check("T19 小幸站内搜索命中",
      len(res) == 1 and "战神4" in res[0]["title"]
      and res[0]["page_url"].endswith("/archives/70.html"),
      f"got={res}")
# 英文名：站内搜索只认中文，经别名表转「战神」再搜
res_en = xd.search("god of war")
check("T19b 小幸英文名走别名转中文",
      len(res_en) == 1 and "战神4" in res_en[0]["title"], f"got={res_en}")
check("T19c 小幸真正无结果时返回空", xd.search("巫师三") == [])
# 全量枚举：翻页 + 去重（首页只列最近 16 条，站点实际 30+——
# 2026-09-13 用户指正；空查询走全量）
_all = xd.search("")
check("T19d 小幸全量枚举翻页去重",
      len(_all) == 3 and any("战神4" in e["title"] for e in _all),
      f"got={len(_all)}")
entries = xd.resolve_downloads("https://www.xiaoxingjie.com/archives/39.html")
check("T20 小幸解析出 MediaFire 直链",
      len(entries) == 1
      and entries[0]["url"].startswith("https://download")
      and ".mediafire.com/" in entries[0]["url"]
      and entries[0]["name"].endswith(".zip")
      and entries[0]["version"] == "2.6.2",
      f"got={entries}")

# ---------- RAR 支持（小幸部分条目为 .rar，2026-09-13 实测「战神4」）----------
from app.downloader.base import _looks_like_rar
from app.security import safe_extract_rar

_rar4 = _TMP / "t4.rar"
_rar4.write_bytes(b"Rar!\x1a\x07\x00\x00" + b"\x00" * 32)
_rar5 = _TMP / "t5.rar"
_rar5.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
_notrar = _TMP / "x.zip"
_notrar.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
check("T21 RAR4/RAR5 魔数识别",
      _looks_like_rar(_rar4) and _looks_like_rar(_rar5)
      and not _looks_like_rar(_notrar))
# 无任何解压工具的场景必须确定化报出（monkeypatch 而非依赖环境巧合：
# 伪 zip 在某些 bsdtar 下可能 -tf 成功列出，改测工具缺失分支）
import app.security as _sec
_orig_find = _sec._find_rar_tool
_sec._find_rar_tool = lambda: None
try:
    _raised = False
    try:
        safe_extract_rar(_notrar, _TMP / "rar_out")
    except ValueError:
        _raised = True
    check("T21b 无解压工具时明确报错降级", _raised)
finally:
    _sec._find_rar_tool = _orig_find

# ---------- 风灵搜索相关性过滤（2026-09-13 用户反馈「剑星」）----------
# 站点"无匹配"时返回一批固定推荐条目（中文查询恒 14 条英文结果），
# 必须过滤，否则中文名搜索显示"有结果但全无关"且不触发英文名重试
from app.downloader.fling import FlingTrainerDownloader

_NOISE = [{"title": "Onimusha: Way of the Sword Trainer", "page_url": "u1"},
          {"title": "Black Myth: Wukong Trainer", "page_url": "u2"}]
check("T22 中文查询滤掉无关英文结果",
      FlingTrainerDownloader._filter_relevant(_NOISE, "剑星") == [])
check("T22b 英文查询按词保留命中",
      len(FlingTrainerDownloader._filter_relevant(
          [{"title": "Stellar Blade Trainer", "page_url": "u3"}] + _NOISE,
          "Stellar Blade")) == 1)
check("T22c 空查询原样返回",
      len(FlingTrainerDownloader._filter_relevant(_NOISE, "")) == 2)
# 停用词/单词巧合不得放行（2026-09-13 审查 P2-1 实测反例，修完的验收标准）
check("T22d 仅停用词命中不成交易",
      FlingTrainerDownloader._filter_relevant(
          [{"title": "Rise of the Tomb Raider Trainer", "page_url": "u"}],
          "Some Random Game Of") == [])
check("T22e 单词巧合不成交易",
      FlingTrainerDownloader._filter_relevant(
          [{"title": "Escape from Tarkov Trainer", "page_url": "u"}],
          "Escape from Duckov") == [])
check("T22f 日文假名查询按整串匹配",
      FlingTrainerDownloader._filter_relevant(_NOISE, "バイオハザード") == [])
check("T22g 多词命中仍保留",
      len(FlingTrainerDownloader._filter_relevant(
          [{"title": "Elden Ring Trainer", "page_url": "u"}] + _NOISE,
          "Elden Ring")) == 1)

# ---------- 多源目录隔离（2026-09-13 审查 P1/P2-3）----------
# 游戏已有 A 源修改器时，B 源的落点必须在 trainers/B/ 之下；
# 跨源同名目录不能互相判冲突
from app.ui.dialogs._common import trainer_dest_dir
from app.config import config as _cfg

libx = Library()
gx, _ = libx.add_game("CyberGame", launch={"type": "file", "value": "C:/g/x.exe"})
_root = _cfg.trainers_root
d_fling = _root / "风灵月影" / "CyberGame"
d_fling.mkdir(parents=True, exist_ok=True)
libx.add_trainer(gx["id"], source="风灵月影", name="T",
                 exe_path=str(d_fling / "t.exe"), dir_path=str(d_fling))
_game_x = libx.get_game(gx["id"])
d_xing = trainer_dest_dir(_game_x, libx, "小幸")
check("T23 跨源不复用他源目录",
      d_xing.parent.name == "小幸" and d_xing != d_fling, f"got={d_xing}")
check("T23b 同源复用原目录",
      trainer_dest_dir(_game_x, libx, "风灵月影") == d_fling)
# 另一游戏在风灵源占用同名目录 → 小幸源下不应被判冲突加后缀
gx2, _ = libx.add_game("CyberGame", launch={"type": "file", "value": "C:/g/y.exe"})
d2 = _root / "风灵月影" / "CyberGame"
libx.add_trainer(gx2["id"], source="风灵月影", name="T2",
                 exe_path=str(d2 / "t2.exe"), dir_path=str(d2))
d_new = trainer_dest_dir(libx.get_game(gx2["id"]), libx, "小幸")
check("T23c 跨源同名目录不误判冲突",
      d_new.parent.name == "小幸" and "(2)" not in d_new.name,
      f"got={d_new.name}")

# ---------- 更新检查的匹配环节（2026-09-13 终轮 P1）----------
# 搜索侧拿到英文名还不够：匹配侧 variants 也要带上，否则纯中文名游戏
# 「搜到了却匹配不上」→ 更新检查仍显示"已是最新"
from app.ui.tasks import _UpdateCheckWorker as _UCW

_rs = [{"title": "Stellar Blade Trainer", "page_url": "u1"}]
check("T24 纯中文名 + 英文名候选可匹配",
      _UCW._best_match_page("剑星", _rs,
                            extra_variants=["剑星", "Stellar Blade"]) == "u1")
check("T24b 不给英文名时纯中文名匹配不上（说明该参数必要）",
      _UCW._best_match_page("剑星", _rs) is None)
check("T24c 含 ASCII 的中文名无需额外候选",
      _UCW._best_match_page(
          "AI LIMIT 无限机兵",
          [{"title": "AI LIMIT Trainer", "page_url": "u2"}]) == "u2")
check("T24d 匹配仍防同系列抢首位",
      _UCW._best_match_page(
          "God of War",
          [{"title": "God of War Ragnarok Trainer", "page_url": "u3"},
           {"title": "God of War Trainer", "page_url": "u4"}]) == "u4")

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

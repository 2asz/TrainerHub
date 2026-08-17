"""游戏与修改器库：JSON 存储（UTF-8、原子写入、脏标记 + UI 层防抖保存）。"""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

LIBRARY_PATH = DATA_DIR / "library.json"


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Library:
    """纯数据层（不依赖 Qt），线程安全；保存由 UI 层防抖驱动。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.games = {}          # game_id -> game dict
        self._dirty = False
        self.last_error = None   # 最近一次保存失败的原因（供 UI 提示，不静默）
        self._revision = 0       # 数据版本号，变更 +1（供 process_watch 缓存失效）
        self.load()

    # ---------- 持久化 ----------
    @staticmethod
    def _normalize_game(gid, game) -> dict | None:
        """规范化单条游戏记录：校验并修复嵌套结构类型。
        记录非法（非 dict / name 非字符串）返回 None；字段类型错误按安全默认修正。"""
        if not isinstance(game, dict) or not isinstance(game.get("name"), str) \
                or not game["name"].strip():
            return None
        out = dict(game)
        # id 必须为合法非空字符串；否则返回 None（丢弃记录）——
        # 空 key 或嵌套 id 为 list 等非法类型会触发 cleaned[id] 的
        # TypeError: unhashable type 崩溃
        gid_candidate = gid if isinstance(gid, str) and gid else out.get("id", gid)
        if not isinstance(gid_candidate, str) or not gid_candidate.strip():
            return None
        out["id"] = gid_candidate
        # process_names 必须为字符串列表
        pns = out.get("process_names")
        out["process_names"] = [p for p in pns if isinstance(p, str)] \
            if isinstance(pns, list) else []
        # trainers 必须为 dict 列表，且每条含 name；补齐缺省键
        # （source/id/exe_path/version 等字段必须为字符串，
        #   否则 {t["source"] ...} 等集合推导会因 source=[] 等触发 TypeError）
        trs = out.get("trainers")
        if not isinstance(trs, list):
            out["trainers"] = []
        else:
            clean = []
            for t in trs:
                if not isinstance(t, dict) or not isinstance(t.get("name"), str) \
                        or not t["name"].strip():
                    continue
                nt = dict(t)
                # 字符串字段：非法类型（list/dict/None 等）重置为默认空串
                for key, default in (("id", "t-" + uuid.uuid4().hex[:12]),
                                     ("source", ""), ("exe_path", ""),
                                     ("version", ""), ("dir_path", "")):
                    val = nt.get(key)
                    if not isinstance(val, str):
                        nt[key] = default
                # 空 trainer.id 会导致启动/管理时按 id 找不到条目，替换为新 id
                if not nt["id"].strip():
                    nt["id"] = "t-" + uuid.uuid4().hex[:12]
                # 布尔字段
                if not isinstance(nt.get("downloaded"), bool):
                    nt["downloaded"] = False
                if not isinstance(nt.get("first_run_confirmed"), bool):
                    nt["first_run_confirmed"] = False
                clean.append(nt)
            out["trainers"] = clean
        # cover_file 必须为字符串（封面线程 Path(cover_file) 遇 list/dict 会 TypeError）
        cf = out.get("cover_file")
        if not isinstance(cf, str):
            out["cover_file"] = None
        # launch.value 必须为字符串（Path(value)/str(value) 遇 list 会 TypeError）
        if isinstance(out.get("launch"), dict):
            lv = out["launch"].get("value")
            if lv is not None and not isinstance(lv, str):
                out["launch"] = None
        return out

    def load(self):
        with self._lock:
            self.games = {}
            if LIBRARY_PATH.exists():
                try:
                    raw = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
                except Exception:
                    self._backup_corrupt()
                    self.games = {}
                else:
                    # 顶层结构必须是 dict（JSON 对象）；数组/标量等视为损坏，
                    # 备份后恢复默认，而不是静默当作空库
                    if not isinstance(raw, dict):
                        self._backup_corrupt()
                        self.games = {}
                        self._dirty = False
                        return
                    games = raw.get("games", {})
                    # schema 校验：games 必须是 dict；非法记录丢弃，
                    # 结构损坏（games 非 dict）则备份后恢复默认
                    if not isinstance(games, dict):
                        self._backup_corrupt()
                        self.games = {}
                    else:
                        cleaned = {}
                        for gid, game in games.items():
                            norm = self._normalize_game(gid, game)
                            if norm is not None:
                                cleaned[norm["id"]] = norm
                        self.games = cleaned
            for game in self.games.values():
                game.setdefault("trainers", [])
                game.setdefault("process_names", [])
            self._dirty = False

    def _backup_corrupt(self):
        """结构损坏的库文件备份为 .corrupt-<时间戳>，避免启动崩溃也便于事后排查。"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            bak = LIBRARY_PATH.with_name(f"library.corrupt-{ts}.json")
            LIBRARY_PATH.replace(bak)
        except Exception:
            pass

    def save(self, force=False) -> bool:
        """持久化到磁盘。返回是否成功；失败原因存 last_error（不静默吞掉）。"""
        with self._lock:
            if not self._dirty and not force:
                return True
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = LIBRARY_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps({"games": self.games},
                                          ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(LIBRARY_PATH)
                self._dirty = False
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = str(e)
                return False

    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    def _mark_dirty(self):
        """标记有改动并递增版本号（需在持有锁时调用）。"""
        self._dirty = True
        self._revision += 1

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    # ---------- 查询 ----------
    def all_games(self) -> list:
        with self._lock:
            return sorted(self.games.values(), key=lambda g: g["name"].casefold())

    def get_game(self, gid):
        with self._lock:
            return self.games.get(gid)

    def all_trainers(self) -> list:
        """返回 [(game_id, trainer)] 扁平列表。"""
        with self._lock:
            out = []
            for gid, game in self.games.items():
                for t in game.get("trainers", []):
                    out.append((gid, t))
            return out

    def prune_missing_trainers(self) -> int:
        """移除 exe 文件已不存在的修改器记录（用户手动删磁盘文件的情况，
        否则卡片/管理页会一直显示已删掉的修改器）。
        exe_path 为空的记录保留（历史数据无法判断）。返回移除数。"""
        removed = 0
        with self._lock:
            for game in self.games.values():
                trs = game.get("trainers", [])
                kept = []
                for t in trs:
                    exe = t.get("exe_path")
                    if exe and not Path(exe).is_file():
                        removed += 1
                        continue
                    kept.append(t)
                if len(kept) != len(trs):
                    game["trainers"] = kept
                    self._mark_dirty()
        return removed

    def trainers_of(self, gid) -> list:
        with self._lock:
            game = self.games.get(gid)
            return list(game["trainers"]) if game else []

    def game_source_set(self) -> set:
        with self._lock:
            return {t.get("source", "") for game in self.games.values()
                    for t in game.get("trainers", [])}

    # ---------- 修改 ----------
    def add_game(self, name, steam_id=None, launch=None, process_names=None,
                 cover_url=None, cover_file=None):
        gid = self._make_gid(steam_id, launch)
        with self._lock:
            existing = self.games.get(gid)
            if existing:
                # 同名同源（同 appid/epic-id）已存在：返回旧记录，不新增
                if name != existing["name"]:
                    existing["name"] = name
                    self._mark_dirty()
                return existing, False
            game = {
                "id": gid,
                "name": name,
                "steam_id": steam_id,
                "launch": launch,            # {"type": "steam"|"file"|"epic", "value": ...}
                "process_names": list(process_names or []),
                "cover_url": cover_url,
                "cover_file": cover_file,
                "trainers": [],
                "added_at": now_str(),
            }
            self.games[gid] = game
            self._mark_dirty()
            return game, True

    @staticmethod
    def _make_gid(steam_id, launch):
        """生成稳定游戏 ID：
        - Steam: steam-<appid>
        - Epic: 从协议 URL 提取 appid → epic-<appid>（防止同一 Epic 游戏反复导入成多份）
        - 本地程序: 按 exe 路径稳定化（同路径不重复）；否则 uuid"""
        if steam_id:
            return f"steam-{steam_id}"
        launch = launch or {}
        if launch.get("type") == "epic" and launch.get("value"):
            m = re.search(r"apps/([^%:\s]+)", str(launch["value"]))
            if m:
                return f"epic-{m.group(1)}"
        if launch.get("type") == "file" and launch.get("value"):
            import hashlib
            path = str(launch["value"]).lower().replace("\\", "/").strip()
            h = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
            return f"file-{h}"
        return "game-" + uuid.uuid4().hex[:12]

    def update_game(self, gid, **fields):
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return False
            changed = False
            for k, v in fields.items():
                if k in ("id", "trainers"):
                    continue
                if game.get(k) != v:
                    game[k] = v
                    changed = True
            # 无实际变化不标记 dirty：避免无谓的防抖保存与进程映射重建
            if changed:
                self._mark_dirty()
            return True

    def change_launch_target(self, gid, launch, steam_id=None):
        """统一修改启动方式并重建稳定 ID（带冲突检查）。
        适用所有类型变更：Steam/Epic/本地 互相切换、本地路径变更、AppID 变更。
        返回 (game, None) 成功 / (None, 错误消息) 失败。
        直接改 launch/steam_id 而不重建 id，会导致重新导入时产生重复记录。"""
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return None, "游戏不存在"
            new_gid = self._make_gid(steam_id, launch)
            if new_gid == gid:
                game["launch"] = launch
                game["steam_id"] = steam_id
                self._mark_dirty()
                return game, None
            if new_gid in self.games:
                return None, "目标启动方式已存在于库中，无法修改（避免重复记录）"
            game["id"] = new_gid
            game["launch"] = launch
            game["steam_id"] = steam_id
            del self.games[gid]
            self.games[new_gid] = game
            self._mark_dirty()
            return game, None

    def change_steam_id(self, gid, new_sid):
        """修改 Steam AppID 并重建游戏 ID（带冲突检查）。"""
        return self.change_launch_target(gid, {"type": "steam", "value": str(new_sid)},
                                         steam_id=str(new_sid))

    def remove_game(self, gid):
        with self._lock:
            if gid in self.games:
                del self.games[gid]
                self._mark_dirty()
                return True
            return False

    def add_trainer(self, gid, *, source, name, exe_path, dir_path=None,
                    version=None, sha256=None, downloaded=False):
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return None
            trainer = {
                "id": "t-" + uuid.uuid4().hex[:12],
                "source": source,
                "name": name,
                "exe_path": str(exe_path),
                "dir_path": str(dir_path) if dir_path else None,
                "version": version or "",
                "sha256": sha256,
                "downloaded": bool(downloaded),      # 官网下载入库
                "first_run_confirmed": False,        # 平衡型安全策略
                "added_at": now_str(),
            }
            game["trainers"].append(trainer)
            self._mark_dirty()
            return trainer

    def update_trainer(self, gid, tid, **fields):
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return False
            for t in game["trainers"]:
                if t["id"] == tid:
                    for k, v in fields.items():
                        if k != "id":
                            t[k] = v
                    self._mark_dirty()
                    return True
            return False

    def remove_trainer(self, gid, tid):
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return False
            before = len(game["trainers"])
            game["trainers"] = [t for t in game["trainers"] if t["id"] != tid]
            if len(game["trainers"]) != before:
                self._mark_dirty()
                return True
            return False

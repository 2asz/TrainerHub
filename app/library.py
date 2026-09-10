"""游戏与修改器库：JSON 存储（UTF-8、原子写入、脏标记 + UI 层防抖保存）。

损坏恢复链路：load() 发现文件损坏 → 先把坏文件改名留证（.corrupt-*）→
按修改时间倒序逐个尝试 library.backup-*.json，第一个能解析且非空的直接恢复，
并在 load_note 里说明（主窗口据此弹窗告知用户）；全部失败才清空，
且把原因写进 last_error（UI 可见，绝不静默）。
备份轮转永远跳过空库——防止一次损坏后，空库把好备份逐步挤掉。"""
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

LIBRARY_PATH = DATA_DIR / "library.json"


def _snapshots_backup_files() -> list:
    """按 (修改时间, 文件名) 排序的备份文件列表：纯文件名字典序会把
    library.backup-legacy.json 排在最后当成"最新"。"""
    return sorted(LIBRARY_PATH.parent.glob("library.backup-*.json"),
                  key=lambda p: (p.stat().st_mtime_ns, p.name))


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Library:
    """纯数据层（不依赖 Qt），线程安全；保存由 UI 层防抖驱动。

    查询方法（all_games/get_game/all_trainers/trainers_of）返回深拷贝——
    调用方拿到的永远是快照，直接改它不会生效也不会污染库内数据；
    所有写入必须走 add_game/update_game/add_trainer 等带脏标记的方法。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.games = {}          # game_id -> game dict
        self._dirty = False
        self.last_error = None   # 最近一次保存失败/库损坏的原因（供 UI 提示，不静默）
        self.load_note = None    # 本次 load 的恢复说明（如"已从备份恢复"），供 UI 告知
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
        # launch.value 必须为字符串（Path(value)/str(value) 遇 list 会 TypeError）；
        # launch 本身非 dict（手改/坏备份写成字符串）也置 None——否则
        # launch_game/process_watch 的 launch.get(...) 会连环 AttributeError
        if isinstance(out.get("launch"), dict):
            lv = out["launch"].get("value")
            if lv is not None and not isinstance(lv, str):
                out["launch"] = None
        elif out.get("launch") is not None:
            out["launch"] = None
        # 数值/时间/ID 字段守门（与 update_game 对称）：手改或他人备份导入的
        # 脏值会让详情面板 int() 崩、卡片墙排序 TypeError（2026-09-02 审查）
        for key in ("play_count", "play_seconds"):
            val = out.get(key)
            if val is not None:
                try:
                    out[key] = int(val)
                except (TypeError, ValueError):
                    out[key] = 0
        for key in ("last_played", "steam_id", "cover_url"):
            val = out.get(key)
            if val is not None and not isinstance(val, str):
                out[key] = str(val)
        return out

    def load(self):
        with self._lock:
            self.games = {}
            self.load_note = None
            # revision 只需单调变化、不能归零：运行期 load（设置页导入）会把
            # 内存重置，若归零会与 process_watch 启动时记录的 _built_revision(0)
            # 撞车 → 映射缓存不重建，运行检测静默用旧数据（2026-09-02 审查 P1-1）
            self._revision += 1
            self.last_error = None
            corrupted = False
            need_persist = False
            if LIBRARY_PATH.exists():
                raw = None
                try:
                    with open(LIBRARY_PATH, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                except Exception:
                    corrupted = True
                # 顶层结构必须是 dict（JSON 对象）；数组/标量等视为损坏
                if raw is not None and not isinstance(raw, dict):
                    corrupted = True
                    raw = None
                games = raw.get("games") if isinstance(raw, dict) else None
                if games is None and isinstance(raw, dict):
                    games = {}       # 合法 JSON 但缺 games 键：按空库处理
                if games is not None and not isinstance(games, dict):
                    corrupted = True
                    games = None
                if corrupted:
                    # 坏文件先改名留证（.corrupt-<ts>），再尝试从备份恢复——
                    # 只清空不恢复 = 一次损坏丢全部数据（2026-08-31 险些发生）
                    self._corrupt_saved = self._backup_corrupt()
                    games = self._restore_from_backup()   # 恢复失败返回 None
                    if games is not None:
                        # 恢复成功必须尽快落盘：坏文件已被改名，磁盘上此刻
                        # 没有 library.json，若不保存，用户纯浏览后关窗
                        # （防抖保存因不脏而跳过）下次启动数据全部丢失
                        #（2026-09-03 发布前审查 P0，实测复现）。
                        # 真正写盘在函数尾部 games 装入内存之后
                        need_persist = True
                if corrupted and self.load_note and self._corrupt_saved:
                    self.load_note += "；损坏的原文件已另存为 library.corrupt-*.json"
                if games is not None:
                    cleaned = {}
                    for gid, game in games.items():
                        norm = self._normalize_game(gid, game)
                        if norm is None:
                            continue
                        if norm["id"] in cleaned:
                            # 重复 id（如手工编辑过库文件）：静默覆盖会让
                            # 数据无痕消失，至少留一条日志
                            from . import audit
                            audit.warning(
                                f"游戏库存在重复 id {norm['id']!r}"
                                f"（源键 {gid!r}），仅保留最后一条")
                        cleaned[norm["id"]] = norm
                    self.games = cleaned
                else:
                    # 无可用备份：只能从空库开始，但必须让 UI 知道原因
                    self.games = {}
                    self.last_error = ("游戏库文件损坏，且没有可用的备份可恢复。"
                                       f"损坏文件已另存到 {LIBRARY_PATH.parent}"
                                       " 下的 library.corrupt-*.json")
            for game in self.games.values():
                game.setdefault("trainers", [])
                game.setdefault("process_names", [])
            self._dirty = False
            if need_persist:
                # 恢复数据此刻已在内存，立即写回磁盘（坏文件已改名，盘上无库）
                self._dirty = True
                self.save(force=True)

    def import_replace(self, staged_lib, staged_covers) -> None:
        """原子导入外部备份（设置页「导入游戏库」第二步专用）：
        校验暂存 → 备份当前 → covers 就位 → 替换 library.json → 重载内存。

        锁的边界（2026-09-04 审查 P2-2）：校验与备份/covers 拷贝等重 IO 在
        锁外——持锁数秒会阻塞所有走库锁的 UI 读操作（界面假死）；仅
        「替换 library.json + load()」在锁内最小段完成。磁盘覆盖与内存
        load 之间若插入防抖保存，旧内存会把刚导入的文件写回磁盘，导入被
        静默回滚（2026-09-02 审查 P1-4）——而防抖保存同样要先拿锁，
        最小锁段即可挡住这个窗口。

        失败抛异常；此时 covers 可能已迁入部分新文件（暂存迁移不可整体
        回滚），但 library.json 未动、旧库完好，孤儿封面清理会兜底
        （2026-09-04 审查 P2-3）。"""
        # 锁外：先校验暂存内容——replace 成功后才发现坏数据会走损坏恢复
        # 链路，导入呈"半成功"（2026-09-03 发布前审查 P2）
        try:
            with open(staged_lib, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict) or not isinstance(raw.get("games"), dict):
                raise ValueError("暂存的 library.json 不是有效的游戏库数据")
        except Exception as e:
            raise ValueError(f"导入数据无效: {e}") from e
        # 锁外：当前数据先落盘（含未保存的内存改动）+ 整体备份一份
        self.save(force=True)
        backup_dir = DATA_DIR / ("import-backup-"
                                 + datetime.now().strftime("%Y%m%d-%H%M%S"))
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        if LIBRARY_PATH.exists():
            shutil.copy2(LIBRARY_PATH, backup_dir / LIBRARY_PATH.name)
        covers = DATA_DIR / "covers"
        if covers.is_dir():
            shutil.copytree(covers, backup_dir / "covers", dirs_exist_ok=True)
        covers.mkdir(parents=True, exist_ok=True)
        # 锁外：covers 先拷进暂存目录、拷齐后整体迁入——逐文件直接拷
        # covers/ 会在中途异常时留下半截新封面（2026-09-04 审查 P2-3）
        if staged_covers and Path(staged_covers).is_dir():
            staging = DATA_DIR / "covers.import-tmp"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            try:
                for f in sorted(Path(staged_covers).iterdir()):
                    shutil.copy2(f, staging / f.name)
                for f in sorted(staging.iterdir()):
                    os.replace(f, covers / f.name)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        # 锁内最小段：替换 + 重载，之间防抖保存进不来（同样需要拿锁）
        with self._lock:
            tmp = LIBRARY_PATH.with_suffix(".tmp")
            shutil.copyfile(staged_lib, tmp)
            tmp.replace(LIBRARY_PATH)
            self.load()

    def _restore_from_backup(self) -> dict | None:
        """按修改时间倒序尝试数据目录内的 library.backup-*.json，第一个能
        解析且 games 非空的直接采用。成功时写 load_note；全部失败返回 None。"""
        data_root = LIBRARY_PATH.parent.resolve()
        try:
            # 按 (修改时间, 文件名) 排序：纯文件名字典序会把
            # library.backup-legacy.json 排在最后当成"最新"
            backups = sorted(LIBRARY_PATH.parent.glob("library.backup-*.json"),
                             key=lambda p: (p.stat().st_mtime_ns, p.name))
        except OSError:
            return None
        for bak in reversed(backups):
            # 路径包含校验：只读数据目录内、名字符合约定前缀的备份；
            # bak.name 不可能含分隔符，data_root / name 恒在数据目录内
            name = bak.name
            if not name.startswith("library.backup-") or ".." in name:
                continue
            safe = data_root / name
            if safe.parent != data_root:
                continue
            try:
                with open(safe, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                games = raw.get("games") if isinstance(raw, dict) else None
                if not isinstance(games, dict) or not games:
                    continue
                # 只挑至少有一条能通过校验的备份，跳过同样损坏/为空的
                if not any(self._normalize_game(gid, game) is not None
                           for gid, game in games.items()):
                    continue
            except Exception:
                continue
            self.load_note = (f"游戏库文件损坏，已从备份 {name} 恢复。"
                              "建议检查游戏与修改器列表是否完整")
            return games
        return None

    def _backup_corrupt(self) -> bool:
        """结构损坏的库文件备份为 .corrupt-<时间戳>，避免启动崩溃也便于事后排查。
        只保留最近 10 份留证；返回是否成功（失败时 UI 文案不应声称已留证）。"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            bak = LIBRARY_PATH.with_name(f"library.corrupt-{ts}.json")
            LIBRARY_PATH.replace(bak)
        except Exception:
            return False
        try:
            corrupts = sorted(LIBRARY_PATH.parent.glob("library.corrupt-*.json"),
                              key=lambda p: p.stat().st_mtime_ns)
            for old in corrupts[:-10]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def save(self, force=False) -> bool:
        """持久化到磁盘。返回是否成功；失败原因存 last_error（不静默吞掉）。

        原子写入三件套：写临时文件 → flush + os.fsync 落盘 → replace 原子替换。
        断电/蓝屏时最坏情况是旧的 library.json 原样保留（损坏恢复链路兜底），
        不会出现 0 字节/截断文件。"""
        with self._lock:
            if not self._dirty and not force:
                return True
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                payload = json.dumps({"games": self.games},
                                     ensure_ascii=False, indent=2)
                tmp = LIBRARY_PATH.with_suffix(".tmp")
                tmp.write_text(payload, encoding="utf-8")
                # fsync 落盘（断电保护）：write_text 拿不到 fd，单独开读写
                # 句柄同步——Windows 的 FlushFileBuffers 要求写权限，
                # 只读句柄会 EBADF。fsync 本身可能失败（exFAT/网络盘/杀软
                # 占用），失败只留痕不阻断——数据已写入 tmp，别让防断电的
                # 手段反过来弄丢这次保存（2026-09-03 发布前审查 P1）
                try:
                    fno = os.open(tmp, os.O_RDWR)
                    try:
                        os.fsync(fno)
                    finally:
                        os.close(fno)
                except OSError as e:
                    from . import audit
                    audit.warning(f"library fsync 失败（不阻断保存）: {e}")
                # 备份轮转：写入前把上一份完整数据留作带时间戳的备份，
                # 保留最近 _BACKUP_KEEP 份（library.backup.json 兼容保留）。
                # 单份备份在两份同时损坏时无路可退，多份更稳。
                # 空库不轮转的两面性：损坏被清空后若继续轮转，空库会逐步挤掉
                # 好备份；代价是用户**合法清空**库时也不产生新备份——此后若
                # 文件损坏，恢复会"复活"清空前的游戏（有意取舍，知情为准）
                if LIBRARY_PATH.exists() and LIBRARY_PATH.stat().st_size > 0 \
                        and self.games:
                    try:
                        self._rotate_backups()
                    except OSError:
                        pass
                tmp.replace(LIBRARY_PATH)
                self._dirty = False
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = str(e)
                return False

    _BACKUP_KEEP = 5

    def _rotate_backups(self):
        """把当前库文件留作带时间戳的备份，保留最近 _BACKUP_KEEP 份。
        10 分钟窗口内不新建也不覆盖快照——此前窗口命中会把最新快照覆盖为
        当前内容，防抖 2 秒的密集保存下"最新备份"恒等于当前状态，某次
        保存携带坏数据时唯一的新快照被当场污染（2026-09-03 发布前审查）。
        旧版固定名 library.backup.json 首次轮转时把**它的内容**并入序列。"""
        backups = _snapshots_backup_files()
        legacy = LIBRARY_PATH.with_name("library.backup.json")
        if not legacy.exists() and backups \
                and time.time() - backups[-1].stat().st_mtime < 600:
            return                     # 复用窗口内：保留原快照，不新建不覆盖
        if legacy.exists():
            # 旧版固定名的内容并入时间戳序列后**本轮结束**（不另存当前
            # 快照——dst 复用会把刚并进来的旧内容当场覆盖掉，
            # 2026-09-04 审查 P1 实锤）；下一次轮转再存当前快照
            dst = LIBRARY_PATH.with_name("library.backup-legacy.json")
            shutil.copyfile(legacy, dst)
            try:
                legacy.unlink()
            except OSError:
                pass
            backups = _snapshots_backup_files()
            for old in backups[:max(0, len(backups) - self._BACKUP_KEEP)]:
                old.unlink(missing_ok=True)
            return
        if backups and time.time() - backups[-1].stat().st_mtime < 600:
            return                     # 复用窗口内：保留原快照，不新建不覆盖
        dst = LIBRARY_PATH.with_name(
            "library.backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".json")
        shutil.copyfile(LIBRARY_PATH, dst)
        backups = _snapshots_backup_files()
        for old in backups[:max(0, len(backups) - self._BACKUP_KEEP)]:
            old.unlink(missing_ok=True)

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
    # 以下查询一律返回深拷贝：调用方拿到的是快照，改它既不会生效也不会
    # 污染库内数据（此前返回内部引用，靠"只读约定"约束——后台线程误改
    # 不触发落盘就静默丢数据；load() 整体替换 games 后旧引用还会变孤儿）。
    # 数百条记录的规模下 deepcopy 成本可忽略（实测 <1ms/百条）。

    def all_games(self) -> list:
        with self._lock:
            # 排序键 str() 兜底：name 被写坏成非 str 时不能崩掉全部调用方
            games = sorted(self.games.values(),
                           key=lambda g: str(g.get("name") or "").casefold())
            return copy.deepcopy(games)

    def get_game(self, gid):
        """返回游戏记录的深拷贝快照（只读用法无需感知差异；
        所有写入必须走 update_game / add_trainer / update_trainer 等）。"""
        with self._lock:
            game = self.games.get(gid)
            return copy.deepcopy(game) if game is not None else None

    def all_trainers(self) -> list:
        """返回 [(game_id, trainer)] 扁平列表（trainer 为深拷贝）。"""
        with self._lock:
            out = []
            for gid, game in self.games.items():
                for t in game.get("trainers", []):
                    out.append((gid, copy.deepcopy(t)))
            return out

    def prune_missing_trainers(self) -> int:
        """移除 exe 文件已不存在的修改器记录（用户手动删磁盘文件的情况，
        否则卡片/管理页会一直显示已删掉的修改器）。
        exe_path 为空的记录保留（历史数据无法判断）。返回移除数。"""
        # 磁盘探测在锁外：慢速/网络盘上逐文件 is_file 若持锁，会阻塞
        # 所有走库锁的读操作（2026-09-09 审查 P3-7）
        with self._lock:
            probe = [(game["id"], t["id"], t.get("exe_path") or "")
                     for game in self.games.values()
                     for t in game.get("trainers", [])]
        dead = {(gid, tid) for gid, tid, exe in probe
                if exe and not Path(exe).is_file()}
        if not dead:
            return 0
        # 两次锁段之间新增/恢复的记录不在 dead 里，不会被误删；
        # 已删游戏也不会再出现在第二段的遍历里
        removed = 0
        with self._lock:
            for game in self.games.values():
                trs = game.get("trainers", [])
                kept = [t for t in trs
                        if (game["id"], t["id"]) not in dead]
                if len(kept) != len(trs):
                    game["trainers"] = kept
                    self._mark_dirty()
                    removed += len(trs) - len(kept)
        return removed

    def trainers_of(self, gid) -> list:
        with self._lock:
            game = self.games.get(gid)
            return copy.deepcopy(game["trainers"]) if game else []

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
                return copy.deepcopy(existing), False
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
            # 深拷贝返回（与查询方法同一契约）：调用方改返回值不影响库内数据
            return copy.deepcopy(game), True

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
                # 字段类型守门（与 load 的 _normalize_game 对称）：
                # 脏值挡在门外而不是存进库等读取时崩
                if k == "name":
                    if not isinstance(v, str) or not v.strip():
                        continue          # 空名/非字符串：丢弃（name 是排序/查找主键）
                elif k == "launch":
                    if v is not None and not isinstance(v, dict):
                        continue
                elif k == "steam_id":
                    if v is not None and not isinstance(v, str):
                        v = str(v)
                elif k in ("cover_file", "cover_url", "last_played") \
                        and v is not None and not isinstance(v, str):
                    v = str(v)
                elif k == "process_names":
                    v = [p for p in (v or []) if isinstance(p, str)] \
                        if isinstance(v, list) else []
                elif k in ("play_count", "play_seconds"):
                    try:
                        v = int(v or 0)
                    except (TypeError, ValueError):
                        continue          # 非数字：丢弃该字段，不写脏值
                if game.get(k) != v:
                    game[k] = v
                    changed = True
            # 无实际变化不标记 dirty：避免无谓的防抖保存与进程映射重建
            if changed:
                self._mark_dirty()
            return True

    def touch_played(self, gid):
        """记录游玩：last_played = 当前时间（ISO），play_count +1。
        供「最近游玩」分类/排序使用。游戏不存在时静默忽略。"""
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return
            game["last_played"] = now_str()
            game["play_count"] = int(game.get("play_count") or 0) + 1
            self._mark_dirty()

    def add_play_seconds(self, gid, seconds):
        """累加游戏时长（秒）。由主窗口在游戏进程退出时调用（会话计时）。
        游戏不存在 / seconds 非正数时静默忽略。"""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return
        if seconds <= 0:
            return
        with self._lock:
            game = self.games.get(gid)
            if not game:
                return
            game["play_seconds"] = int(game.get("play_seconds") or 0) + seconds
            self._mark_dirty()

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
                return copy.deepcopy(game), None
            if new_gid in self.games:
                return None, "目标启动方式已存在于库中，无法修改（避免重复记录）"
            game["id"] = new_gid
            game["launch"] = launch
            game["steam_id"] = steam_id
            del self.games[gid]
            self.games[new_gid] = game
            self._mark_dirty()
            return copy.deepcopy(game), None

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
                    version=None, sha256=None, downloaded=False, note=""):
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
                "note": str(note or ""),             # 用户备注（快捷键等）
                "added_at": now_str(),
            }
            game["trainers"].append(trainer)
            self._mark_dirty()
            return copy.deepcopy(trainer)

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

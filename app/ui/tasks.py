"""后台任务集合（从 main_window 拆出）：全部 QThread worker + 启动清理函数。

新手视角：每个 QThread 子类都是一条"后台流水线"——重活（网络/磁盘扫描/
哈希）在 run() 里做，结果用 Signal 发回主线程（UI 只能在主线程改）。
统一的取消契约：request_cancel() 置位 threading.Event，run() 循环里检查。
统一的命名禁忌：自定义完成信号不能叫 finished（会覆盖 QThread.finished，
导致 deleteLater 在线程结束前被排队）。
"""
import difflib
import queue
import re
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import audit
from ..install_info import (_steam_installed_appids, epic_app_name_from_launch,
                            epic_display_name)
from .cover_loader import _cover_cache_path


class _SteamImportWorker(QThread):
    """后台线程：解析快捷方式 + 并行拉取 appinfo，逐条发回主线程。

    之前二进制对快捷方式逐个串行调 Steam 官方接口（每个还带超时+重试退避），
    几十个快捷方式非常慢；现在用线程池并行拉取（网络 IO 密集，多线程收益大）。
    found_steam(appid, fallback_name, info)：info 为 None 时 UI 用文件名兜底入库。"""
    found_steam = Signal(str, str, object)
    found_lnk = Signal(str, dict)
    progress = Signal(int, int)
    import_finished = Signal(int)   # 不能叫 finished：会覆盖 QThread.finished

    _MAX_WORKERS = 6        # 并行拉取 Steam API 的线程数

    def __init__(self, library, app_info, folder, parent=None):
        super().__init__(parent)
        self._library = library
        self._app_info = app_info
        self._folder = folder
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    @staticmethod
    def _process_file(f, app_info, stop):
        """处理单个快捷方式（线程池 worker）：解析 + 拉 appinfo。
        返回统一 dict；无法识别返回 None。"""
        from ..steam_import import parse_shortcut
        try:
            parsed = parse_shortcut(f)
        except Exception:
            return None
        if not parsed:
            return None
        stem = Path(f.name).stem
        if "appid" in parsed:
            try:
                info = app_info.fetch(parsed["appid"], cancel=stop)
            except Exception:
                # 单条失败不中止整个导入，按未知处理（UI 用文件名兜底）
                info = None
            return {"kind": "steam", "appid": parsed["appid"],
                    "stem": stem, "info": info}
        if parsed.get("launch"):
            return {"kind": "lnk", "stem": stem, "launch": parsed["launch"]}
        return None

    def run(self):
        """并行拉取 appinfo：用 daemon Thread + 队列而不是 ThreadPoolExecutor——
        后者的线程会在解释器退出时被 atexit 回调 join，Steam API 卡住时
        可能吊死退出进程；daemon 线程不阻塞进程退出。"""
        folder = Path(self._folder)
        files = []
        try:
            if folder.is_dir():
                files = sorted(
                    f for f in folder.iterdir()
                    if f.suffix.lower() in (".url", ".lnk"))
        except OSError:
            pass
        total = len(files)
        done = ok = 0
        todo = queue.Queue()
        results = queue.Queue()

        def _worker():
            while True:
                f = todo.get()
                if f is None:          # 哨兵：退出
                    return
                try:
                    res = self._process_file(f, self._app_info, self._stop)
                except Exception:
                    res = None
                results.put(res)

        workers = [threading.Thread(target=_worker, daemon=True)
                   for _ in range(min(self._MAX_WORKERS, len(files)) or 1)]
        for w in workers:
            w.start()
        try:
            for f in files:
                todo.put(f)
            while done < total and not self._stop.is_set():
                try:
                    res = results.get(timeout=0.2)
                except queue.Empty:
                    continue
                done += 1
                if res is None:
                    self.progress.emit(done, ok)
                    continue
                if res["kind"] == "steam":
                    info = res["info"]
                    if info and info.get("name"):
                        ok += 1
                    self.found_steam.emit(res["appid"], res["stem"], info)
                else:
                    ok += 1
                    self.found_lnk.emit(res["stem"], res["launch"])
                self.progress.emit(done, ok)
        finally:
            # 取消/提前结束：给每个 worker 投喂哨兵让其退出（尚在跑的单个
            # 请求带超时 + 取消检查，daemon 线程不阻塞进程退出）
            for _ in workers:
                todo.put(None)
            # 无论正常结束还是异常/取消，都必须发送 finished，否则进度框永不关闭。
            # 发已识别数（ok）而不是文件总数：未识别/取消的场景 UI 才不夸大
            self.import_finished.emit(ok)


class _SteamAcfImportWorker(QThread):
    """Steam 全量导入：读全部 appmanifest_*.acf（本地文件，零网络），
    把库里还没有的已安装游戏逐个发回主线程入库。

    复用 _SteamImportWorker 的 found_steam 信号契约（appid, 名称, info=None）：
    主线程 _import_steam_result 会按 ACF 名称入库；中文名与封面后续由
    _schedule_cover_fetch 的官方封面链路顺带补齐。
    StateFlags 不做过滤（只记日志）——Steam 对更新中/云同步中的游戏给出的
    标志位五花八门，按位过滤会漏掉正常游戏。"""
    found_steam = Signal(str, str, object)   # appid, ACF 名称, None
    progress = Signal(int, int)              # 已处理, 新增数
    import_finished = Signal(int)            # 新增数（不能叫 finished：会覆盖内建信号）

    def __init__(self, library, games, parent=None):
        super().__init__(parent)
        self._library = library
        self._games = games            # 入口处扫描好的 appmanifest 清单
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        games = self._games
        # 预过滤：库里已有的 Steam 游戏（按 appid）不再发回主线程重复入库
        existing = set()
        for g in self._library.all_games():
            launch = g.get("launch") or {}
            if launch.get("type") == "steam":
                sid = str(g.get("steam_id") or launch.get("value") or "")
                if sid:
                    existing.add(sid)
        pending = [g for g in games if g["appid"] not in existing]
        not_full = sum(1 for g in games if not g["state_flags"] & 4)
        if not_full:
            audit.info(f"Steam 全量导入：{not_full} 款游戏 StateFlags≠4"
                       "（更新中/未完成），不做过滤照常导入")
        added = 0
        for i, g in enumerate(pending):
            if self._stop.is_set():
                break
            self.found_steam.emit(g["appid"], g["name"], None)
            added += 1
            self.progress.emit(i + 1, added)
        self.import_finished.emit(added)


class _ScanWorker(QThread):
    """后台扫描文件夹中的疑似修改器。
    仅做纯数据收集，进度/结果/错误经信号发回主线程（UI 只能在主线程操作）。"""
    progress = Signal(int)          # 已识别候选数
    failed = Signal(str)            # 扫描失败原因（权限/磁盘错误等）
    scan_finished = Signal(int)     # 候选总数（不能叫 finished：会覆盖内建信号）

    def __init__(self, folder, cancel, parent=None):
        super().__init__(parent)
        self._folder = folder
        self._cancel = cancel
        self._results = []

    @property
    def results(self) -> list:
        return self._results

    def request_cancel(self):
        """关闭窗口等场景：置位取消标记，扫描循环会在下一个文件处停止。"""
        self._cancel.set()

    def run(self):
        from ..scanner import scan_folder
        try:
            for p in scan_folder(self._folder, cancel=self._cancel):
                self._results.append(str(p))
                self.progress.emit(len(self._results))
        except Exception as e:
            # 权限/磁盘等错误不再静默当作"没有结果"，上报给 UI 显示原因
            self.failed.emit(f"{type(e).__name__}: {e}")
        self.scan_finished.emit(len(self._results))


class _CoverFetchWorker(QThread):
    """后台为无封面游戏补拉封面：Steam（按 appid 拉 appdetails）+ Epic（按名搜）。
    命中发 cover_found；确定无结果发 cover_miss；网络失败发 cover_fail
    （主线程据此区分负缓存与网络冷却）。"""
    cover_found = Signal(str, str)     # gid, cover_url
    cover_miss = Signal(str)           # gid（API 正常但确定无结果）
    cover_fail = Signal(str)           # gid（网络失败，下次再试）

    def __init__(self, games, parent=None):
        super().__init__(parent)
        self._games = games            # [(gid, name, kind, steam_id, launch_value)]
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..epic_cover import search_epic_covers
        from ..steam_import import get_app_info
        api = get_app_info()   # 共享实例：双实例会并发写同一缓存文件（P3-8）
        try:
            for gid, name, kind, steam_id, launch_value in self._games:
                if self._stop.is_set():
                    break
                try:
                    if kind == "steam" and steam_id:
                        info = api.fetch(str(steam_id), cancel=self._stop)
                        if info and info.get("cover_url"):
                            self.cover_found.emit(gid, info["cover_url"])
                        elif info:
                            self.cover_miss.emit(gid)
                        else:
                            self.cover_fail.emit(gid)
                    elif kind == "epic":
                        # 用多个候选名搜索：库里中文名 + Epic 清单 DisplayName。
                        # AppName 提取用 install_info 的统一实现（URL 编码兼容）
                        queries = [name]
                        app_name = epic_app_name_from_launch(launch_value)
                        if app_name:
                            dn = epic_display_name(app_name)
                            if dn and dn != name:
                                queries.append(dn)
                        results = search_epic_covers(queries, timeout=12,
                                                     cancel=self._stop)
                        if results:
                            self.cover_found.emit(gid, results[0]["cover_url"])
                        else:
                            self.cover_fail.emit(gid)   # 请求失败/无结果均按可重试
                    else:
                        self.cover_miss.emit(gid)
                except Exception as e:
                    audit.warning(f"官方封面查询异常 {gid} {name}: {e}")
                    self.cover_fail.emit(gid)
        finally:
            api.close()


class _UpdateCheckWorker(QThread):
    """后台检查「官网下载」修改器的最新版本：按游戏名搜官网 → 解析最新版号。
    命中 newer 的发 found（批量一次性上报），完成发 done(检查数, 失败数)。"""
    found = Signal(list)              # [{gid, tid, game, cur, new, entry, page_url}]
    progress = Signal(int, int)       # 已检查, 总数
    done = Signal(int, int)

    def __init__(self, library, adapter, parent=None, only=None):
        super().__init__(parent)
        self._library = library
        self._adapter = adapter
        self._only = only             # (gid, tid)：只检查这一个（详情面板用）；None=全部
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..downloader.fling import FlingTrainerDownloader as F
        # 只查风灵源的修改器：其他源（小幸等）的版本不在 fling 官网比对，
        # 混进来会拿错版本基准（小幸源的更新检查暂未实现，见审查记录）
        tasks = [(gid, t) for gid, t in self._library.all_trainers()
                 if t.get("downloaded")
                 and (t.get("source") or "") == F.SOURCE
                 and (self._only is None or (gid, t["id"]) == self._only)]
        total = len(tasks)
        outdated, fails = [], 0
        for i, (gid, t) in enumerate(tasks):
            if self._stop.is_set():
                break
            game = self._library.get_game(gid)
            if not game:
                continue
            self.progress.emit(i + 1, total)
            try:
                # 中文游戏名先搜全名，搜不到再退回纯 ASCII 名（如
                # "AI LIMIT 无限机兵" → "AI LIMIT"，官网标题是英文）
                results, tried = self._search_results(
                    self._adapter, game["name"], game.get("steam_id"))
                # 用过的查询词一并参与匹配：纯中文名游戏靠 Steam 英文名
                # 才能与英文标题对上（2026-09-13 终轮 P1）
                page_url = self._best_match_page(game["name"], results,
                                                 extra_variants=tried)
                if not page_url:
                    fails += 1        # 搜索结果与游戏名对不上，不能盲目更新
                    audit.info(f"更新检查无匹配页: {game['name']}")
                    continue
                entries = self._adapter.resolve_downloads(page_url)
                if not entries:
                    fails += 1
                    audit.info(f"更新检查页面无下载链接: {game['name']} {page_url}")
                    continue
                latest = entries[0]
                if F._version_key(latest.get("version", "")) \
                        > F._version_key(t.get("version", "")):
                    outdated.append({
                        "gid": gid, "tid": t["id"], "game": game["name"],
                        "cur": t.get("version", ""), "new": latest.get("version", ""),
                        "entry": latest, "page_url": page_url,
                    })
            except Exception as e:
                fails += 1
                # 失败必须留痕，否则"显示最新"会掩盖网络/解析问题
                audit.warning(
                    f"更新检查失败 {game['name']}: {type(e).__name__}: {e}")
        if outdated:
            self.found.emit(outdated)
        self.done.emit(total, fails)

    @staticmethod
    def _search_results(adapter, game_name, steam_appid=None):
        """官网搜索：依次尝试 游戏全名 → 纯 ASCII 名 → Steam 官方英文名。
        英文名推导与下载对话框同一套（2026-09-13 审查 P1：批量更新检查
        此前没有英文名回退，中文名游戏恒搜不到 → 永远显示"已是最新"）。
        SteamAppInfo 自带缓存，重复检查不会反复打 API。
        返回 (按页面 URL 去重合并的结果, 实际用过的查询词列表)——
        查询词要一并交给 _best_match_page 做匹配，否则纯中文名游戏
        "搜到了却匹配不上"（2026-09-13 终轮 P1）。"""
        queries = [game_name]
        ascii_only = " ".join(re.sub(r"[^\x00-\x7F]+", " ", game_name or "").split())
        if ascii_only and ascii_only != game_name:
            queries.append(ascii_only)
        sid = str(steam_appid or "")
        if sid.isdigit() and getattr(adapter, "NEEDS_ENGLISH_NAME", False):
            try:
                from ..steam_import import get_app_info
                en = (get_app_info().fetch_english_name(sid) or "").strip()
                if en and en not in queries:
                    queries.append(en)
            except Exception:
                pass          # 英文名查询失败不阻断（继续试已有候选）
        merged, seen = [], set()
        for q in queries:
            try:
                for r in adapter.search(q):
                    u = r.get("page_url")
                    if u and u not in seen:
                        seen.add(u)
                        merged.append(r)
            except Exception:
                continue      # 单次搜索失败不中断整体检查
        return merged, queries

    @staticmethod
    def _best_match_page(game_name, results, extra_variants=None) -> str | None:
        """从搜索结果中选与游戏名最匹配的页面（不盲取第一个——
        搜索排序常把同系列其他作品排前面，导致误判"已最新"）。
        归一化（小写去符号）后：互含=强匹配；分数相同时用相似度作为决胜
        （例如 "God of War" vs "God of War Ragnarok" 互含分相同，
          但后者与 God of War 的相似度更低，应选 God of War Trainer 2022）。
        支持中文名：同时用"全名"和"纯 ASCII 部分"参与匹配
        （如 "AI LIMIT 无限机兵" 的 ASCII 部分是 "AI LIMIT"）。
        extra_variants：搜索实际用过的候选词（如 Steam 英文名）——纯中文名
        游戏靠它才能匹配英文标题（2026-09-13 终轮：此前只补了搜索侧、
        没补匹配侧，更新检查仍失效）。
        低于阈值返回 None。"""
        def norm(s):
            return "".join(c for c in (s or "").lower() if c.isalnum())

        variants = [norm(game_name)]
        ascii_part = re.sub(r"[^\x00-\x7F]", "", game_name or "")
        v2 = norm(ascii_part)
        if v2 and v2 not in variants:
            variants.append(v2)
        for extra in extra_variants or []:
            vn = norm(extra)
            if vn and vn not in variants:
                variants.append(vn)

        best_score, best_url, best_ratio = 0.0, None, 0.0
        for r in results or []:
            b = norm(r.get("title", ""))
            if not b:
                continue
            # 该结果在"全名/ASCII 名"里取最高分
            cur_score, cur_ratio = 0.0, 0.0
            for a in variants:
                if not a:
                    continue
                ratio = difflib.SequenceMatcher(None, a, b).ratio()
                if a == b:
                    score = 1.0            # 完全一致（最强）
                elif a in b or b in a:
                    score = 0.9            # 互含（次强）
                else:
                    score = ratio
                if score > cur_score or (score == cur_score and ratio > cur_ratio):
                    cur_score, cur_ratio = score, ratio
            # 分数相同用相似度决胜（防同系列作品抢首位）
            if cur_score > best_score or (cur_score == best_score
                                          and cur_ratio > best_ratio):
                best_score, best_url, best_ratio = cur_score, r.get("page_url"), cur_ratio
        return best_url if best_score >= 0.6 else None


class _UpdateInstallWorker(QThread):
    """按更新清单逐个安装最新版（串行，带进度与取消）。"""
    progress = Signal(int, int, str)          # 已完成, 总数, 游戏名
    one_done = Signal(dict, dict)             # info, item
    all_done = Signal(int, int)               # 成功, 失败

    def __init__(self, library, adapter, items, parent=None):
        super().__init__(parent)
        self._library = library
        self._adapter = adapter
        self._items = items
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        ok = fail = 0
        total = len(self._items)
        for i, item in enumerate(self._items):
            if self._stop.is_set():
                break
            game = self._library.get_game(item["gid"])
            trainer = None
            for t in (game or {}).get("trainers", []):
                if t["id"] == item["tid"]:
                    trainer = t
                    break
            if not game or not trainer:
                fail += 1
                continue
            try:
                dest = self._dest_root(game, trainer)
                info = self._adapter.install(
                    game["name"], item["page_url"], dest,
                    cancel=self._stop, entry=item["entry"])
                self.one_done.emit(info, item)
                ok += 1
            except Exception as e:
                fail += 1
                # 失败必须留痕：用户只看到"失败 N 个"，没有日志无法排查
                audit.warning(
                    f"更新安装失败 {game['name']}: {type(e).__name__}: {e}")
            self.progress.emit(i + 1, total, game["name"])
        self.all_done.emit(ok, fail)

    def _dest_root(self, game, trainer):
        """更新落点：与下载/手动添加一致——复用已有目录，仅他游戏同名占用时避让。"""
        from .dialogs import trainer_dest_dir
        return trainer_dest_dir(game, self._library, self._adapter.SOURCE)


# ---------------------------------------------------------------- 启动清理
def _prune_missing_games(library):
    """启动清理：启动目标已不存在的游戏移除库记录——
    Steam 游戏清单消失（已卸载）/ 本地 exe 被删。
    无法判断的保留（Steam 未安装 / Epic 协议类）。
    与「删除游戏」一致：仅删记录，不动磁盘修改器文件。返回移除的游戏名列表。

    Steam 清单数量骤减（< 上次快照的 90%）时跳过本轮清理：Steam 装了 ≠
    清单完整（重装未刷新 / 外接盘未挂载 / 新库未同步），清单不完整时
    直接删记录 = 无确认误删全部游戏（2026-09-03 发布前审查 P1）。"""
    import json
    from ..config import DATA_DIR
    steam_ok, installed = _steam_installed_appids()
    snapshot = DATA_DIR / "steam_apps_snapshot.json"
    if steam_ok:
        prev = None
        try:
            if snapshot.exists():
                prev = json.loads(snapshot.read_text(encoding="utf-8"))
        except Exception:
            prev = None
        # 快照防线对"空集合"同样生效：glob 全部失败（Steam 更新锁盘/杀软
        # 占用）时 installed 为空集，不做保护会把所有 Steam 游戏判 dead
        #（2026-09-04 审查：空集缺口）
        if isinstance(prev, int) and len(installed) < prev * 0.9:
            audit.warning(
                f"Steam 已安装清单数量骤减（{prev} → {len(installed)}），"
                "跳过本轮 Steam 游戏卸载清理（本地 exe 游戏仍照常清理），"
                "防止清单不完整时误删游戏记录")
            steam_ok = False      # 本轮不做 Steam 判定；快照也不更新（保守）
        elif installed:
            try:
                snapshot.write_text(json.dumps(len(installed)), encoding="utf-8")
            except OSError:
                pass
        # installed 为空且无快照（无法判断是否真的全卸载）：也按跳过处理
        elif prev is None:
            audit.warning("Steam 已安装清单为空且无历史快照，"
                          "跳过本轮 Steam 游戏卸载清理（本地 exe 游戏仍照常清理）")
            steam_ok = False
    removed = []
    for g in library.all_games():
        launch = g.get("launch") or {}
        t = launch.get("type")
        dead = False
        if t == "steam":
            sid = str(g.get("steam_id") or launch.get("value") or "")
            if steam_ok and sid and sid not in installed:
                dead = True
        elif t == "file":
            v = launch.get("value")
            if v and not Path(v).is_file():
                dead = True
        if dead:
            library.remove_game(g["id"])
            removed.append(g["name"])
    return removed


def _prune_missing_covers(library) -> int:
    """清理"cover_file 指向的文件已被删除"的失效引用。

    用户手动删掉 data/covers/ 后，library.json 里还留着旧路径；
    如果不清掉，封面补拉逻辑会以为"有封面"而跳过，导致永远不重新生成。
    返回清理条数。"""
    n = 0
    for g in library.all_games():
        cf = g.get("cover_file")
        if cf and not Path(cf).is_file():
            library.update_game(g["id"], cover_file=None)
            n += 1
    return n


def _migrate_manual_covers(library) -> int:
    """一次性迁移（后台线程执行）：旧版本把用户手选封面存成 <sha(gid)>.png，
    与网络封面磁盘缓存同名——官方封面下载成功会把手选图覆盖掉。
    迁移：复制为 manual-<sha(gid)>.png 并更新 cover_file 引用；
    旧文件保留（它可能同时承载官方封面缓存）。返回迁移条数。"""
    import shutil
    n = 0
    for g in library.all_games():
        cf = g.get("cover_file")
        if not cf:
            continue
        p = Path(cf)
        if p.name.startswith("manual-") or not p.is_file():
            continue
        if p != _cover_cache_path(g["id"]):
            continue      # 非 gid 键名（offline- 等离线封面），无需迁移
        dst = p.parent / ("manual-" + p.name)
        try:
            if not dst.exists():
                shutil.copy2(p, dst)
            library.update_game(g["id"], cover_file=str(dst))
            n += 1
        except OSError:
            continue
    return n


class _StartupCleanWorker(QThread):
    """启动清理后台线程：磁盘已删修改器 / 已卸载游戏 / 失效封面引用。

    之前这三步在 MainWindow.__init__ 主线程里跑：Steam 清单解析 + 逐个
    is_file() 检查，游戏多时首启会卡住界面。现在丢后台线程跑，
    界面先显示出来，清理完发信号由主线程刷新模型。库操作方法带锁，线程安全。
    支持取消（关窗时 closeEvent 统一 request_cancel）——此前没有取消方法，
    游戏多时关窗会白等满 10 秒超时。"""
    done = Signal(int, int, int)      # 移除修改器数, 移除游戏数, 失效封面数

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        pruned = gone = stale = 0
        if not self._stop.is_set():
            try:
                pruned = self._library.prune_missing_trainers()
            except Exception:
                pruned = 0
        if not self._stop.is_set():
            try:
                gone = len(_prune_missing_games(self._library))
            except Exception:
                gone = 0
        if not self._stop.is_set():
            try:
                stale = _prune_missing_covers(self._library)
            except Exception:
                stale = 0
        migrated = 0
        if not self._stop.is_set():
            try:
                migrated = _migrate_manual_covers(self._library)
            except Exception:
                migrated = 0
        if migrated:
            audit.info(f"启动迁移：{migrated} 个手选封面改存 manual- 命名（防被官方封面覆盖）")
        self.done.emit(pruned, gone, stale)


class _OfflineCoverWorker(QThread):
    """后台离线封面生成：给没有有效 cover_file 的游戏生成 exe 图标/首字母封面。

    之前在主线程跑：每款游戏提取 exe 图标（~70ms）+ Steam ACF 解析，
    几十款游戏首启就卡死。现在在后台线程生成（covers.py 已改纯 QImage，
    线程安全），主线程只收 (gid, 封面路径) 结果更新库与卡片。"""
    cover_done = Signal(str, str)     # gid, 封面路径（失败为空串）
    batch_done = Signal()

    def __init__(self, games, parent=None):
        super().__init__(parent)
        self._games = games            # 游戏字典快照
        self._stop = threading.Event()

    def request_cancel(self):
        self._stop.set()

    def run(self):
        from ..covers import generate_cover_for_game
        for g in self._games:
            if self._stop.is_set():
                break
            try:
                cover = generate_cover_for_game(g)
            except Exception as e:
                audit.warning(f"离线封面生成失败 {g.get('id')} {g.get('name')}: {e}")
                cover = None
            self.cover_done.emit(g.get("id", ""), cover or "")
        self.batch_done.emit()

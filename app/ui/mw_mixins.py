"""MainWindow 的方法组 Mixin（从 main_window 拆出，按职责物理分离）。

- IoMixin：导入 / 扫描 / 封面调度 / 数据清理（与外部 IO 打交道的方法组）；
- UpdateMixin：修改器更新链（检查 / 提示 / 安装 / 收尾）。

为什么用 Mixin 而不是独立 Manager 类：这些方法深度依赖 MainWindow 的
UI 成员与 worker 清理契约，用 Mixin 物理分离代码、保持 self 语义不变，
是风险最小的拆法。MainWindow 声明
class MainWindow(IoMixin, UpdateMixin, QMainWindow) 后方法运行时解析。

【宿主契约】本模块方法依赖 MainWindow 具备以下成员（无运行时校验，
拆分/改名时需两侧同步）：
  库与视图：_library / _model / _covers / _detail；
  任务设施：_app_info / _fling / _import_dlg / _start_worker_with_progress；
  界面元素：statusBar()、_upd_hint（UpdateMixin）。
方法内引用的 worker 类与清理函数来自 app.ui.tasks / 本模块 _clear_worker_attr。
"""
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFileDialog, QMessageBox, QProgressDialog)

from .. import audit
from ..config import DATA_DIR
from .tasks import (_SteamImportWorker, _SteamAcfImportWorker, _ScanWorker,
                    _CoverFetchWorker, _OfflineCoverWorker, _UpdateCheckWorker,
                    _UpdateInstallWorker, _prune_missing_covers)
from .dialogs import ScanResultDialog


def _clear_worker_attr(obj, attr, worker):
    """QThread finished 后清空成员引用，避免对已 deleteLater 对象调 isRunning()。
    模块级函数：IoMixin / UpdateMixin 共用，不隐式依赖另一个 Mixin 的方法
    （MRO 上少继承一个就会 AttributeError）。"""
    if getattr(obj, attr, None) is worker:
        setattr(obj, attr, None)


def _worker_running(worker) -> bool:
    """线程是否仍在运行。None（从未启动）或 C++ 对象已被 deleteLater 析构
    均按"未在运行"处理——否则首次运行的 None.isRunning() 抛 AttributeError，
    已析构对象的 isRunning() 抛 RuntimeError（连点「导入 Steam」「刷新封面」
    等场景可复现）。"""
    if worker is None:
        return False
    try:
        return worker.isRunning()
    except RuntimeError:
        return False


class IoMixin:
    """导入 / 扫描 / 封面调度 / 孤儿清理方法组。"""

    def _start_worker_with_progress(self, worker, title, clear_attr,
                                    wire=None, total=None):
        """进度框 + worker 接线统一模板（此前 5 处逐行重复，是最容易
        漏改出竞态/顺序坑的地方）：
        - 创建模态进度框（setMinimumDuration 必须在 show 之前，否则不生效）；
        - wire(dlg)：调用方连接该 worker 特有的信号（各 worker 的完成信号
          名/签名不同：import_finished / scan_finished / done / all_done）；
        - 取消按钮接 request_cancel；结束后先清成员引用、后 deleteLater
          （顺序全项目统一：防对已析构对象调 isRunning）；
        - total 非 None 时进度框按数值进度（_upd_install 用）。返回进度框。"""
        dlg = QProgressDialog(title, "取消", 0, 0 if total is None else total,
                              self)
        dlg.setMinimumDuration(300)
        dlg.show()   # 必须 show：不 setValue 的进度对话框永远不会自动显示
        dlg.setWindowModality(Qt.WindowModal)
        if wire is not None:
            wire(dlg)
        dlg.canceled.connect(worker.request_cancel)
        worker.finished.connect(
            lambda ww=worker: _clear_worker_attr(self, clear_attr, ww))
        worker.finished.connect(worker.deleteLater)   # 内建信号：线程结束后释放
        setattr(self, clear_attr, worker)
        worker.start()
        return dlg

    # ------------------------------------------------------------ Steam 导入
    def import_steam(self):
        # 入口二选一：本机装了 Steam（能读到 appmanifest 清单）时优先提供
        # 全量导入；用户拒绝或读不到清单时走旧的快捷方式文件夹导入
        if self._offer_acf_import():
            return
        folder = QFileDialog.getExistingDirectory(self, "选择包含游戏快捷方式的文件夹")
        if folder:
            self._import_folder = folder
            self._import_was_auto = False
            self._start_import()

    def _offer_acf_import(self) -> bool:
        """检测本机已安装的 Steam 游戏（读 appmanifest_*.acf，零网络）。
        有游戏时询问导入方式：选「全量导入」返回 True（已启动导入）；
        选否或读不到清单返回 False，走快捷方式流程。"""
        try:
            from ..install_info import steam_installed_games
            installed = steam_installed_games()
        except Exception as e:
            audit.warning(f"Steam 清单读取失败: {e}")
            return False
        if not installed:
            return False
        ret = QMessageBox.question(
            self, "导入 Steam",
            f"检测到本机 Steam 已安装 {len(installed)} 款游戏。\n\n"
            "Yes：一次性导入全部已安装游戏（推荐，无需快捷方式）\n"
            "No：从快捷方式文件夹导入（适合只挑几款）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ret != QMessageBox.Yes:
            return False
        self._start_acf_import(installed)
        return True

    def _start_acf_import(self, games):
        """启动 Steam 全量导入：后台逐个经 found_steam 信号回主线程入库
        （复用快捷方式导入的入库槽与进度对话框）。games 为入口处扫描好的
        appmanifest 清单，worker 内不再重复读盘。"""
        task = getattr(self, "_acf_import_task", None)
        if _worker_running(task):
            return
        if _worker_running(getattr(self, "_import_task", None)):
            self.statusBar().showMessage("已有快捷方式导入在进行中，请稍候…", 3000)
            return
        task = _SteamAcfImportWorker(self._library, games, self)

        def _wire(dlg):
            task.found_steam.connect(self._import_steam_result)
            task.progress.connect(self._import_progress)
            task.import_finished.connect(self._acf_import_finished)

        self._import_dlg = self._start_worker_with_progress(
            task, "正在读取 Steam 游戏清单…", "_acf_import_task", wire=_wire)

    def _acf_import_finished(self, added):
        """全量导入收尾：关进度框、刷新模型、补封面，按新增数提示。"""
        if self._import_dlg is not None:
            self._import_dlg.close()
            self._import_dlg = None
        self._model.reload()          # 批量完成后一次刷新
        self._mark_save()
        self._schedule_cover_fetch()   # 新导入的无封面游戏补拉封面
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间：状态已更新，弹窗跳过
        if added:
            audit.info(f"Steam 全量导入：新增 {added} 款游戏")
            QMessageBox.information(self, "导入完成", f"已导入 {added} 款 Steam 游戏。")
        else:
            QMessageBox.information(self, "导入完成",
                                    "Steam 游戏都已在库中，无新增。")

    def _maybe_auto_import(self):
        """桌面 game 文件夹有快捷方式且库为空时自动导入（懒加载，不阻塞启动）。"""
        if self._library.all_games():
            return
        for sub in ("game", "游戏", "Games"):
            p = Path.home() / "Desktop" / sub
            try:
                if p.is_dir() and any(f.suffix.lower() in (".url", ".lnk")
                                      for f in p.iterdir()):
                    self._import_folder = p
                    self._import_was_auto = True
                    QTimer.singleShot(400, self, self._start_import)
                    return
            except OSError:
                continue      # 目录不可读等情况跳过，不能挡启动

    def _start_import(self):
        task = getattr(self, "_import_task", None)
        # 两条导入链（快捷方式 / Steam 全量）共用 _import_dlg，互斥防覆盖
        if _worker_running(task):
            return
        if _worker_running(getattr(self, "_acf_import_task", None)):
            self.statusBar().showMessage("已有 Steam 全量导入在进行中，请稍候…", 3000)
            return
        folder = getattr(self, "_import_folder", None)
        if not folder:
            return
        task = _SteamImportWorker(self._library, self._app_info, folder, self)

        def _wire(dlg):
            task.found_steam.connect(self._import_steam_result)
            task.found_lnk.connect(self._import_lnk_result)
            task.progress.connect(self._import_progress)
            task.import_finished.connect(self._import_finished)

        self._import_dlg = self._start_worker_with_progress(
            task, "正在导入游戏…", "_import_task", wire=_wire)

    def _import_progress(self, total, ok):
        dlg = self._import_dlg
        if dlg is not None and dlg.isVisible():
            dlg.setLabelText(f"正在导入游戏…（已识别 {ok}/{total}）")

    def _import_steam_result(self, appid, fallback_name, info):
        # API 拿不到名字时用快捷方式文件名兜底入库，绝不静默丢弃
        name = (info or {}).get("name") or fallback_name
        self._library.add_game(name, steam_id=appid,
                               launch={"type": "steam", "value": appid},
                               cover_url=(info or {}).get("cover_url"))

    def _import_lnk_result(self, name, launch):
        self._library.add_game(name, launch=launch,
                               process_names=[Path(str(launch["value"])).name])

    def _import_finished(self, total):
        if self._import_dlg is not None:
            self._import_dlg.close()
            self._import_dlg = None
        self._model.reload()          # 批量完成后一次刷新
        self._mark_save()
        self._schedule_cover_fetch()   # 新导入的无封面游戏补拉封面
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间：状态已更新，弹窗跳过
        if getattr(self, "_import_was_auto", False):
            self._import_was_auto = False
            return
        if total == 0:
            QMessageBox.information(self, "导入完成", "未找到可识别的游戏快捷方式。")
        else:
            QMessageBox.information(self, "导入完成", f"已导入 {total} 个游戏。")

    # ------------------------------------------------------------ 拖拽导入
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._import_dropped(paths)

    def _import_dropped(self, paths):
        """拖拽导入：.exe 直接入库；.lnk/.url 经快捷方式解析（Steam AppID /
        Epic 协议 / 本地程序+参数）。封面统一走 _schedule_cover_fetch 后台
        生成（主线程逐个提取图标会卡界面）。"""
        from ..steam_import import parse_shortcut
        added = 0
        for s in paths:
            p = Path(s)
            try:
                if p.suffix.lower() == ".exe" and p.is_file():
                    self._library.add_game(
                        p.stem, launch={"type": "file", "value": str(p)},
                        process_names=[p.name])
                    added += 1
                elif p.suffix.lower() in (".lnk", ".url"):
                    parsed = parse_shortcut(p)
                    if not parsed:
                        continue
                    stem = p.stem
                    if "appid" in parsed:
                        self._library.add_game(
                            stem, steam_id=parsed["appid"],
                            launch={"type": "steam", "value": parsed["appid"]})
                    elif parsed.get("launch"):
                        l = parsed["launch"]
                        self._library.add_game(
                            stem, launch=l,
                            process_names=[Path(str(l["value"])).name])
                    else:
                        continue
                    added += 1
            except Exception:
                continue
        if added:
            self._model.reload()
            self._mark_save()
            self._schedule_cover_fetch()
            self.statusBar().showMessage(f"已导入 {added} 款游戏", 5000)
        else:
            self.statusBar().showMessage(
                "没有可识别的游戏文件（支持 .exe / .lnk / .url）", 5000)

    # ------------------------------------------------------------ 扫描
    def scan_trainers(self):
        folder = QFileDialog.getExistingDirectory(self, "选择要扫描的文件夹")
        if not folder:
            return
        if getattr(self, "_scanning", False):
            return
        self._scanning = True
        self._scan_error = ""      # 每次新扫描重置，避免继承上次的错误
        self._scan_cancel = threading.Event()

        # 后台线程只收集结果，UI 更新全部走信号回主线程（避免跨线程操作 Qt 对象）
        worker = _ScanWorker(folder, self._scan_cancel, self)

        def _wire(dlg):
            worker.progress.connect(lambda n: self._scan_progress(dlg, n))
            worker.failed.connect(lambda err: self._scan_failed(dlg, err))
            worker.scan_finished.connect(
                lambda total: self._scan_done(dlg, worker, total))

        self._start_worker_with_progress(
            worker, "正在扫描修改器…", "_scan_worker", wire=_wire)

    def _scan_progress(self, dlg, n):
        if dlg.isVisible():
            dlg.setLabelText(f"已识别 {n} 个候选…")

    def _scan_failed(self, dlg, err):
        self._scan_error = getattr(self, "_scan_error", "") or ""
        if not self._scan_error:
            self._scan_error = err
        if dlg.isVisible():
            dlg.setLabelText(f"扫描遇到错误：{err}")

    def _scan_done(self, dlg, worker, total):
        self._scanning = False
        # 关窗收尾期间（closeEvent 等待循环经 processEvents 派发完成信号）：
        # 只收掉进度框，不再弹结果框——窗口正在析构，弹窗重入会打断收尾
        if getattr(self, "_closing", False):
            try:
                dlg.close()
            except RuntimeError:
                pass
            return
        # 必须在 close() 之前读取消标记：QProgressDialog.close() 会触发
        # canceled → request_cancel → _scan_cancel 置位（实测复现），
        # 先 close 后读会恒走"取消"分支，扫描结果永远不显示
        user_canceled = self._scan_cancel.is_set()
        dlg.close()
        if user_canceled:
            self._scan_error = ""      # 取消分支也清空，防残留错误影响下次扫描
            return
        err = getattr(self, "_scan_error", "") or ""
        self._scan_error = ""
        results = worker.results
        if err and not results:
            # 扫描遇错且无任何结果：明确告知失败原因，而非显示"未发现修改器"
            QMessageBox.warning(self, "扫描失败",
                                f"扫描过程中发生错误，未获得结果：\n{err}")
            return
        if err and results:
            QMessageBox.warning(self, "扫描部分完成",
                                f"部分目录扫描出错（已忽略）：\n{err}")
        if not results:
            QMessageBox.information(self, "扫描完成", "未发现疑似修改器。")
            return
        scan_dlg = ScanResultDialog(self._library, results, self)
        if scan_dlg.exec():
            self._model.reload()
            self._mark_save()

    # ------------------------------------------------------------ 封面补拉
    _COVER_FETCH_BUDGET = 10           # 每轮最多查询数（防大量游戏刷爆请求）
    _COVER_FAIL_TTL_S = 1800           # 网络失败冷却（30 分钟内不重复请求）
    _COVER_RETRY_MS = 300000           # 周期重试扫描（网络恢复后自动补上）

    def _schedule_cover_fetch(self, regenerate_offline=False):
        """为无封面游戏补封面（懒加载，不阻塞 UI）。
        - 所有类型（本地/Steam/Epic）：先**后台线程**离线生成 exe 图标封面；
          找不到图标时用游戏名首字母兜底（生成是重活，丢线程跑，首启不卡）；
        - Steam/Epic：如果还没有 cover_url，再在后台查询官方封面；查到后
          由于加载器优先使用 cover_url，高清官方封面会自动覆盖离线图标。
        regenerate_offline：手动"刷新封面"时为 True——已有的 offline-* 封面
        也强制重新生成（旧封面可能是 exe 解析失败时代的首字母兜底，
        能力恢复后应升级），官方封面缓存不受影响。"""
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间启动定时器仍会触发，不再起新 worker
        if _worker_running(getattr(self, "_offline_cover_worker", None)) \
                or _worker_running(getattr(self, "_cover_fetch_worker", None)):
            # 上一轮还在跑：延后合并，而不是把新游戏的封面任务直接丢弃
            #（丢弃后要等 5 分钟重试定时器，2026-09-09 审查 P3-3）
            QTimer.singleShot(1000, self, self._schedule_cover_fetch)
            return
        # 一次全库快照供离线段与官方段共用（all_games 是全量深拷贝，
        # 两次调用成本翻倍——2026-09-04 审查优化项）
        snapshot = self._library.all_games()
        # 1) 离线封面：后台线程生成，主线程只收结果更新库与卡片。
        #    判断"有没有封面"要看文件是否真的存在：用户删过 data/covers/ 后，
        #    cover_file 字段可能还指向已删除的文件，必须视为无封面重新生成。
        try:
            pending = []
            for g in snapshot:
                cf = g.get("cover_file")
                if cf and Path(cf).is_file():
                    # 已有封面文件：默认跳过；手动刷新时 offline-* 强制重生成
                    if not (regenerate_offline
                            and Path(cf).name.startswith("offline-")):
                        continue
                # 浅拷贝：all_games 已是深拷贝快照，这里再拷一层游戏本体，
                # 确保工作线程持有的结构与主线程此后任何 update_game 无共享
                pending.append(dict(g))
            if pending:
                w = getattr(self, "_offline_cover_worker", None)
                if not _worker_running(w):
                    w = _OfflineCoverWorker(pending, self)
                    w.cover_done.connect(self._offline_cover_done)
                    w.batch_done.connect(self._offline_cover_batch_done)
                    w.finished.connect(
                        lambda ww=w: _clear_worker_attr(self, "_offline_cover_worker", ww))
                    w.finished.connect(w.deleteLater)
                    self._offline_cover_worker = w
                    w.start()
        except Exception as e:
            audit.warning(f"封面补拉调度异常: {e}")

        # 2) 后台查询官方封面：针对还没有 cover_url 的 Steam/Epic 游戏。
        #    用户手选封面（manual-*）的游戏跳过：显示层手选优先，查了也用不上
        now = time.time()
        fails = getattr(self, "_cover_fail_cache", {})
        negs = getattr(self, "_cover_neg_cache", {})   # 本会话已确认无结果
        games = []
        for g in snapshot:
            if g.get("cover_url"):
                continue
            cf = g.get("cover_file")
            if cf and Path(cf).name.startswith("manual-"):
                continue
            kind = (g.get("launch") or {}).get("type")
            if kind not in ("steam", "epic"):
                continue
            if now - fails.get(g["id"], 0) < self._COVER_FAIL_TTL_S:
                continue
            if g["id"] in negs:   # API 正常但确定没结果：不再反复查询
                continue
            # launch_value：Epic 需要从协议 URL 提取 AppName，才能读清单里的
            # DisplayName 辅助搜索官方封面
            launch_value = str((g.get("launch") or {}).get("value") or "")
            games.append((g["id"], g["name"], kind, g.get("steam_id"),
                          launch_value))
            if len(games) >= self._COVER_FETCH_BUDGET:
                break
        if not games:
            return
        if _worker_running(getattr(self, "_cover_fetch_worker", None)):
            return
        w = _CoverFetchWorker(games, self)
        w.cover_found.connect(self._cover_fetch_found)
        w.cover_miss.connect(self._cover_fetch_miss)
        w.cover_fail.connect(self._cover_fetch_fail)
        w.finished.connect(lambda: _clear_worker_attr(self, "_cover_fetch_worker", w))
        w.finished.connect(w.deleteLater)
        self._cover_fetch_worker = w
        w.start()

    def _start_cover_retry_timer(self):
        """周期重试：网络恢复/加速器开启后，无封面游戏会自动补上（无需重启）。"""
        t = QTimer(self)
        t.setInterval(self._COVER_RETRY_MS)
        t.timeout.connect(self._schedule_cover_fetch)
        t.start()
        self._cover_retry_timer = t

    def _offline_cover_done(self, gid, path):
        """后台线程生成好一张离线封面：主线程更新库记录并让卡片立即加载。"""
        if gid and path:
            self._library.update_game(gid, cover_file=path)
            self._covers.forget(gid)
            self._model.cover_updated(gid)

    def _offline_cover_batch_done(self):
        """离线封面整批完成：落盘一次即可。"""
        self._mark_save()

    def _cover_fetch_found(self, gid, url):
        """主线程：把拉到的官方封面 URL 写入游戏记录并刷新卡片。

        这里不再因为已有离线 cover_file 就跳过：cover_url 优先级高于 cover_file，
        写入后封面加载器会自动展示更清晰的官方封面。
        """
        game = self._game(gid)
        if not game or game.get("cover_url"):
            return
        self._library.update_game(gid, cover_url=url)
        # 官方 cover_url 出现，清除旧的失败/兜底缓存，让高清封面立即生效
        self._covers.forget(gid)
        self._model.cover_updated(gid)
        self._mark_save()

    def _cover_fetch_miss(self, gid):
        """确定无结果（API 正常）：长 TTL 负缓存，本会话内不再请求。

        离线图标/首字母封面已在 _schedule_cover_fetch 中生成，所以这里只需要
        登记负缓存，避免反复查询同一款游戏。
        """
        if not hasattr(self, "_cover_neg_cache"):
            self._cover_neg_cache = {}
        self._cover_neg_cache[gid] = time.time()

    def _cover_fetch_fail(self, gid):
        """网络失败（可能被墙/加速器未开）：冷却后周期重试，不污染负缓存。"""
        if not hasattr(self, "_cover_fail_cache"):
            self._cover_fail_cache = {}
        self._cover_fail_cache[gid] = time.time()

    def _cleanup_orphan_covers(self):
        """数据卫生：删除 data/covers/ 里不再被任何游戏引用、且超过 24 小时
        未修改的封面缓存（游戏被删除/重置 id 后残留）。引用集合 = 每个游戏的
        四种可能命名（sha256 名 / manual- 手选名 / offline- 名 / 旧 gid 名）
        + cover_file 实际路径。结果写审计日志，失败静默。"""
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间启动定时器仍会触发，不再起新 worker
        from .cover_loader import cover_hash
        covers = DATA_DIR / "covers"
        if not covers.is_dir():
            return
        allowed = set()
        for g in self._library.all_games():
            gid = g["id"]
            h = cover_hash(gid)
            allowed.add(f"{h}.png")
            allowed.add(f"manual-{h}.png")
            allowed.add(f"offline-{h}.png")
            allowed.add(f"{gid}.png")
            cf = g.get("cover_file")
            if cf:
                p = Path(cf)
                if p.parent == covers:
                    allowed.add(p.name)
        removed = 0
        now = time.time()
        try:
            for f in covers.iterdir():
                if not f.is_file() or f.name in allowed:
                    continue
                try:
                    if now - f.stat().st_mtime < 86400:   # 24h 内的不动
                        continue
                    f.unlink()
                    removed += 1
                except OSError:
                    continue
        except OSError:
            return
        if removed:
            audit.info(f"孤儿封面清理：删除 {removed} 个无引用缓存")

    # ------------------------------------------------------------ 刷新封面
    def refresh_covers(self):
        """手动"刷新封面"按钮：

        1. 清理失效的 cover_file 引用（用户删过 data/covers/ 的场景）；
        2. 清空封面加载器缓存/失败队列；
        3. 清空官方封面的失败冷却与负缓存，立刻重试；
        4. 重新跑 _schedule_cover_fetch：离线封面重新生成 + 官方封面重新查询。
        适合：网络恢复/加速器打开后，一键把没拉到的官方封面补回来。
        """
        stale = _prune_missing_covers(self._library)
        if stale:
            audit.info(f"刷新封面：清理 {stale} 条失效封面引用")
            self._mark_save()
        self._covers.clear_all()
        self._cover_fail_cache = {}
        self._cover_neg_cache = {}
        # regenerate_offline=True：offline-* 封面（exe 图标/首字母）全部重新
        # 生成——旧封面可能是 exe 解析失败时代的首字母兜底，能力恢复后应升级
        self._schedule_cover_fetch(regenerate_offline=True)
        self._model.reload()
        self._sync_ui_state()
        self.statusBar().showMessage(
            "正在刷新封面：重新生成离线封面 + 重试官方封面…", 4000)


class UpdateMixin:
    """修改器更新链方法组：检查 → 提示 → 安装 → 收尾。"""

    # ------------------------------------------------------------ 检查更新
    def check_trainer_updates(self):
        """手动检查官网下载修改器的最新版本（后台），有新版可一键全部更新。"""
        if not any(t.get("downloaded") for _, t in self._library.all_trainers()):
            QMessageBox.information(
                self, "检查更新",
                "库中没有官网下载的修改器。\n"
                "（仅官网下载的修改器可查更新；手动添加/扫描的没有版本号，无法比较）")
            return
        if _worker_running(getattr(self, "_upd_check_worker", None)):
            self.statusBar().showMessage("已有更新检查在进行中，请稍候…", 3000)
            return
        self._upd_result = []
        w = _UpdateCheckWorker(self._library, self._fling, self)

        def _wire(dlg):
            w.progress.connect(lambda i, n: self._upd_check_progress(dlg, i, n))
            w.found.connect(self._on_updates_found)
            w.done.connect(lambda n, f: self._upd_check_done(dlg, n, f))

        self._start_worker_with_progress(
            w, "正在检查修改器更新…", "_upd_check_worker", wire=_wire)

    def check_trainer_updates_for(self, gid, tid):
        """详情面板「↻ 更新」：只检查这一个官网下载修改器，
        有新版走与全局检查相同的确认→安装链路。"""
        trainer = next((t for t in self._library.trainers_of(gid)
                        if t["id"] == tid), None)
        if not trainer:
            return
        if not trainer.get("downloaded"):
            QMessageBox.information(
                self, "检查更新",
                "该修改器是本地添加的，没有官网版本号可比（更新请重新下载后重新添加）。")
            return
        if _worker_running(getattr(self, "_upd_check_worker", None)):
            self.statusBar().showMessage("已有更新检查在进行中，请稍候…", 3000)
            return
        self._upd_result = []
        w = _UpdateCheckWorker(self._library, self._fling, self, only=(gid, tid))

        def _wire(dlg):
            w.progress.connect(lambda i, n: self._upd_check_progress(dlg, i, n))
            w.found.connect(self._on_updates_found)
            w.done.connect(lambda n, f: self._upd_single_done(dlg, n, f))

        self._start_worker_with_progress(
            w, "正在检查更新…", "_upd_check_worker", wire=_wire)

    def _upd_single_done(self, dlg, total, fails):
        """单修改器检查完成：没新版不打扰（状态栏一句话），有新版弹确认。"""
        dlg.close()
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间不再弹提示框
        if getattr(self, "_upd_result", None):
            self._prompt_updates(self._upd_result)
            return
        msg = "已是最新版本。" if not fails else \
            "检查失败（多为网络问题或页面未匹配），详情见 data\\audit.log。"
        self.statusBar().showMessage(f"更新检查：{msg}", 4000)

    def _upd_check_progress(self, dlg, i, n):
        if dlg.isVisible():
            dlg.setLabelText(f"正在检查更新…（{i}/{n}）")

    def _on_updates_found(self, items):
        self._upd_result = items
        # 标记可更新（卡片角标 + tooltip）
        for it in items or []:
            self._library.update_trainer(it["gid"], it["tid"],
                                         update_available=True)
        self._model.set_updatable({it["gid"] for it in items or []})

    def _silent_update_check(self):
        """启动后静默检查一次修改器更新：有新版 → 状态栏提示（不打扰）。
        条件：库里有官网下载的修改器、没有正在进行的检查、本次会话未查过。"""
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_upd_silent_done", False):
            return
        if not any(t.get("downloaded") for _, t in self._library.all_trainers()):
            return
        if _worker_running(getattr(self, "_upd_check_worker", None)):
            return
        self._upd_silent_done = True
        self._upd_result = []
        w = _UpdateCheckWorker(self._library, self._fling, self)
        w.found.connect(self._on_updates_found)
        w.done.connect(lambda n, f: self._upd_silent_done_result(n, f))
        w.finished.connect(lambda ww=w: _clear_worker_attr(self, "_upd_check_worker", ww))
        w.finished.connect(w.deleteLater)
        self._upd_check_worker = w
        w.start()

    def _upd_silent_done_result(self, total, fails):
        """静默检查完成：仅更新状态栏提示，不弹窗。"""
        items = self._upd_result or []
        hint = self._upd_hint
        if items:
            # linkActivated 只在含 <a href> 的富文本上触发：纯文本写「点击更新」
            # 是死链（用户点了没反应）。带接收者/样式随主题
            from ..theme import current as T
            hint.setText(f'<a href="update" style="color: {T()["accent"]}; '
                         f'text-decoration: none;">⬆️ {len(items)} 个修改器有新版（点击更新）</a>')
            hint.setToolTip("\n".join(
                f"· {it['game']}：v{it['cur'] or '?'} → v{it['new']}"
                for it in items))
            hint.setVisible(True)
            audit.info(f"静默更新检查：{len(items)} 个修改器有新版")
        else:
            hint.setVisible(False)
            audit.info(f"静默更新检查：全部最新（{total} 个，失败 {fails}）")

    def _upd_hint_clicked(self, link):
        """点击状态栏更新提示：弹出确认对话框并一键更新。"""
        items = getattr(self, "_upd_result", None) or []
        if not items:
            return
        self._prompt_updates(items)
        hint = self._upd_hint
        if not getattr(self, "_upd_result", None):
            hint.setVisible(False)

    def _prompt_updates(self, items):
        # 关窗收尾期间 done 信号仍会派发（ExcludeUserInputEvents 不屏蔽
        # queued 信号），此处弹窗会让用户在关窗流程里起新的安装线程
        if getattr(self, "_closing", False):
            return
        lines = "\n".join(
            f"· {it['game']}：v{it['cur'] or '?'} → v{it['new']}" for it in items)
        ret = QMessageBox.question(
            self, "发现新版本",
            f"以下 {len(items)} 个修改器有新版：\n\n{lines}\n\n是否立即全部更新？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._start_update_install(items)
        self._upd_result = []

    def _upd_check_done(self, dlg, total, fails):
        """手动检查完成：关闭进度对话框，按结果提示/弹更新确认。"""
        dlg.close()
        if getattr(self, "_closing", False):
            return          # 关窗收尾期间不再弹确认框（防重入）
        items = getattr(self, "_upd_result", [])
        if not items:
            if fails:
                QMessageBox.warning(
                    self, "检查更新",
                    f"共检查 {total} 个，其中 {fails} 个查询失败"
                    f"（多为网络问题或页面未匹配），失败原因已写入 data\\audit.log。")
            else:
                # 均最新：隐藏旧提示，否则它还挂着但 _upd_result 已空，
                # 点击无任何反馈（死链，2026-09-09 审查 P3-4）
                self._upd_hint.setVisible(False)
                QMessageBox.information(
                    self, "检查更新",
                    f"所有修改器均已最新（共检查 {total} 个）")
            return
        self._prompt_updates(items)

    # ------------------------------------------------------------ 安装更新
    def _start_update_install(self, items):
        w = _UpdateInstallWorker(self._library, self._fling, items, self)

        def _wire(dlg):
            dlg.setAutoReset(False)   # 最后一项 setValue(max) 会让进度框自动隐藏（闪框）
            w.progress.connect(
                lambda i, n, name: self._upd_install_progress(dlg, i, n, name))
            w.one_done.connect(self._on_trainer_updated)
            w.all_done.connect(lambda ok, f: self._upd_install_done(dlg, ok, f))

        self._start_worker_with_progress(
            w, "正在下载更新…", "_upd_install_worker", wire=_wire,
            total=len(items))

    def _upd_install_progress(self, dlg, i, n, name):
        if dlg.isVisible():
            dlg.setValue(i)
            dlg.setLabelText(f"正在更新：{name}（{i}/{n}）")

    def _on_trainer_updated(self, info, item):
        """单个修改器更新完成：刷新记录（版本/路径/哈希），保留首次确认状态；
        同时删除被替换的旧版 exe（否则旧文件残留在文件夹里，页面又不再显示）。"""
        # 记录更新前的旧 exe 路径（用于更新后清理）
        old_exe = ""
        for t in self._library.trainers_of(item["gid"]):
            if t["id"] == item["tid"]:
                old_exe = t.get("exe_path", "") or ""
                break
        self._library.update_trainer(
            item["gid"], item["tid"],
            exe_path=info["exe_path"], dir_path=info["dir_path"],
            version=info.get("version", ""), sha256=info.get("sha256"),
            url=info.get("url", ""), update_available=False)
        audit.info(f"修改器已更新: {item['game']} → v{info.get('version', '')}")
        # 删除旧版文件（新文件同名覆盖时 old==new，跳过）
        new_path = str(Path(info["exe_path"]).resolve())
        if old_exe and str(Path(old_exe).resolve()).casefold() != new_path.casefold():
            try:
                Path(old_exe).unlink(missing_ok=True)
                audit.info(f"更新后清理旧版文件: {old_exe}")
            except OSError as e:
                audit.warning(f"更新后删除旧版文件失败 {old_exe}: {e}")

    def _upd_install_done(self, dlg, ok, fail):
        dlg.close()
        closing = getattr(self, "_closing", False)
        # 重算角标：仍存在未更新修改器的游戏保留标记
        still = {gid for gid, t in self._library.all_trainers()
                 if t.get("update_available")}
        self._model.set_updatable(still)
        self._upd_hint.setVisible(False)
        self._model.reload()
        self._mark_save()
        msg = f"更新完成：成功 {ok} 个"
        if fail:
            msg += f"，失败 {fail} 个（网络问题，可稍后再试）"
        if not closing:
            QMessageBox.information(self, "更新完成", msg)

"""卡片墙视图层（从 main_window 拆出）：Model / Delegate / View / 侧边栏绘制。

职责边界：
- GameListModel：数据与过滤（搜索词 / 分类 / 运行状态），与界面零耦合；
- CardDelegate：把一条游戏记录"画"成卡片（封面 / 名称 / 标签 / 双按钮）；
- CardView：QListView——卡片摆放、鼠标交互（点卡片 / 点按钮 / 右键 / 键盘流）；
- SidebarDelegate：侧边栏条目（名称 + 右对齐计数 + 分组分隔线）。

新手视角：这是经典 Model/View/Delegate 三件套。界面不直接碰数据——
Qt 通过 rowCount()/data() 问模型，delegate 决定画成什么样，view 决定
摆在哪、怎么响应鼠标。数据一变（dataChanged / modelReset）视图自动刷新。
"""
import difflib

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QSize, QAbstractListModel, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient,
                           QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle,
                               QStyledItemDelegate)

from ..theme import current as T
from ..utils import rel_time as _rel_time, fmt_duration as _fmt_duration
from .cover_loader import CoverLoader

# 卡片标准尺寸（单一定义处：delegate sizeHint 与背景预渲染共用）
CARD_W, CARD_H = 300, 190   # 高度 226→190：按钮改悬停浮现，卡片瘦一圈（一屏多一行）


def _card_rect(visual):
    """卡片在视口内的绘制矩形（外框留 7px 阴影边距）。
    delegate 绘制与鼠标命中必须共用这一份几何，否则按钮"看着能点的地方
    点不动、空白处反而能点"（错位量 = 内边距）。"""
    return visual.adjusted(7, 7, -7, -7)


def _btn_rects(rect):
    """卡片底部两个操作按钮的几何（delegate 绘制与 view 命中共用，保证一致）。"""
    w = rect.width()
    y = rect.top() + rect.height() - 38
    bw = (w - 22 - 10) // 2
    game_btn = QRect(rect.left() + 11, y, bw, 28)
    trainer_btn = QRect(rect.left() + 11 + bw + 10, y, bw, 28)
    return game_btn, trainer_btn


# ---------------------------------------------------------------- 配色
# 颜色统一从 app.theme 读取（深色/浅色主题），这里不再写死十六进制。
# SRC_COLORS 是"来源标签"的徽章色，两套主题通用（浅色下仍清晰）。
SRC_COLORS = {"风灵月影": QColor(255, 176, 32),
              "小幸": QColor(178, 132, 255),
              "本地": QColor(140, 200, 120),
              "其他": QColor(170, 170, 180),
              "小辛": QColor(86, 168, 255)}   # 兼容历史数据


# ---------------------------------------------------------------- 数据模型
class GameListModel(QAbstractListModel):
    """卡片墙的数据模型：持有数据 + 过滤（搜索词/分类）+ 运行状态。

    新手视角：这是 Model/View 架构里的 Model（数据层），QListView 是 View（显示层）。
    界面不会直接碰数据，而是通过 rowCount()/data() 让 Qt"问"模型有几行、每行画什么；
    数据一变（reload/dataChanged），界面自动跟着刷新——数据和显示互不纠缠。"""
    Role_Name = Qt.UserRole + 1
    Role_Running = Qt.UserRole + 2
    Role_GameId = Qt.UserRole + 3
    Role_SourceTags = Qt.UserRole + 4
    Role_TrainerCount = Qt.UserRole + 5
    Role_CoverUrl = Qt.UserRole + 6
    Role_CoverFile = Qt.UserRole + 7
    Role_Updatable = Qt.UserRole + 8
    Role_LastPlayed = Qt.UserRole + 9
    Role_PlayCount = Qt.UserRole + 10
    Role_PlaySeconds = Qt.UserRole + 11

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._games = []
        self._running = set()
        self._updatable = set()      # 有新版的修改器所在游戏（卡片角标）
        self._keyword = ""
        self._source = "全部"
        self._snapshot = []          # 按 revision 缓存的排序快照
        self._snapshot_rev = -1
        self._search_index = {}      # gid -> (小写名, 全拼, 首字母)，随快照重建
        self._row_by_gid = {}        # gid -> 行号（reload 重建，O(1) 定位）
        self._tags = []              # 行号 -> 来源标签列表（reload 重建缓存）
        self.reload()

    def _sorted_games(self):
        """按 Library.revision 缓存排序快照：库未变时复用，避免每次 reload 重排序。
        同时缓存搜索索引（小写名/全拼/首字母），拼音转换只做一次。"""
        rev = self._library.revision
        if rev != self._snapshot_rev:
            self._snapshot = self._library.all_games()
            self._search_index = self._build_search_index(self._snapshot)
            self._snapshot_rev = rev
        return self._snapshot

    @staticmethod
    def _build_search_index(games) -> dict:
        """gid -> (小写名, 全拼, 拼音首字母)。拼音失败按空串兜底。"""
        try:
            from pypinyin import lazy_pinyin
        except Exception:
            lazy_pinyin = None
        idx = {}
        for g in games:
            name = g["name"]
            full, initials = "", ""
            if lazy_pinyin and not name.isascii():
                try:
                    parts = lazy_pinyin(name)
                    full = "".join(parts)
                    initials = "".join(p[0] for p in parts if p)
                except Exception:
                    pass
            idx[g["id"]] = (name.casefold(), full, initials)
        return idx

    def _keyword_match(self, kw: str, gid: str) -> bool:
        """关键词匹配：中文名 / 全拼 / 拼音首字母 子串；长词加模糊相似度兜底。"""
        name, full, initials = self._search_index.get(gid, ("", "", ""))
        if kw in name or (full and kw in full) or (initials and kw in initials):
            return True
        if len(kw) >= 4:
            # 模糊兜底：仅对名字本身（容忍拼写错误，如 cyperpank→cyberpunk）。
            # 不对拼音做模糊——拼音子串已覆盖正常输入，模糊会在不同中文游戏
            # 的全拼间产生误报（saierda vs aierdengfahuan 覆盖率可达 0.85）。
            # 归一按较短一方（输入词与名称长度悬殊时 2M/(la+lb) 会压低高覆盖匹配）。
            if name:
                sm = difflib.SequenceMatcher(None, kw, name)
                m_total = sum(b.size for b in sm.get_matching_blocks())
                if m_total >= 4 and m_total / min(len(kw), len(name)) >= 0.75:
                    return True
        return False

    def reload(self):
        self.beginResetModel()
        # 行集合即将重建：悬停/按下状态指向的行对象随之失效，不清的话
        # delegate 会给"移位后的卡片"画按钮浮层（可见却点不动的幽灵按钮，
        # 2026-09-13 审查 P3）
        self._hover_card = None
        self._hover_btn = None
        self._pressed_btn = None
        kw = self._keyword.strip().casefold()
        src = self._source
        out = []
        for g in self._sorted_games():
            if kw and not self._keyword_match(kw, g["id"]):
                continue
            tags = {t["source"] for t in g.get("trainers", [])}
            if src == "无修改器":
                if tags:
                    continue
            elif src == "运行中":
                if g["id"] not in self._running:
                    continue
            elif src == "最近游玩":
                if not g.get("last_played"):
                    continue
            elif src != "全部" and src not in tags:
                continue
            out.append(g)
        if src == "最近游玩":
            out.sort(key=lambda g: g.get("last_played") or "", reverse=True)
        self._games = out
        # 行定位与标签缓存随 reload 重建：运行状态/更新角标/封面刷新都是
        # 单 gid 更新，此前线性扫描找行在冷启动滚动时是 O(n²)
        self._row_by_gid = {g["id"]: i for i, g in enumerate(out)}
        self._tags = [sorted({t["source"] for t in g.get("trainers", [])})
                      for g in out]
        self.endResetModel()

    def set_keyword(self, kw):
        kw = kw.strip().casefold()
        if kw != self._keyword:
            self._keyword = kw
            self.reload()

    def set_source(self, src):
        if src != self._source:
            self._source = src
            self.reload()

    def set_running(self, gid, running):
        # 状态先写入全库集合（与当前分类过滤无关），
        # 切回"全部"后 reload() 仍能正确显示运行中徽章
        if (gid in self._running) == running:
            return
        if running:
            self._running.add(gid)
        else:
            self._running.discard(gid)
        if self._source == "运行中":
            self.reload()          # 该分类视图需要增删行
            return
        i = self._row_by_gid.get(gid)
        if i is not None:
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx, [self.Role_Running])

    def running_count(self) -> int:
        return len(self._running)

    def set_updatable(self, gids):
        """标记有新版的修改器所在游戏（更新检查结果），卡片显示角标。"""
        new = set(gids)
        if new == self._updatable:
            return
        old = self._updatable
        self._updatable = new
        for gid in (new - old) | (old - new):
            i = self._row_by_gid.get(gid)
            if i is not None:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [self.Role_Updatable])

    def remove_updatable(self, gid):
        """清除单个游戏的更新角标（该修改器已重下/已是最新后调用，
        2026-09-13 审查 P3：此前检查结果只置位不清理，橙点永久残留）。"""
        if gid not in self._updatable:
            return
        self._updatable.discard(gid)
        i = self._row_by_gid.get(gid)
        if i is not None:
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx, [self.Role_Updatable])

    def cover_updated(self, gid):
        """封面变化后的单行刷新。
        调用契约：调用方必须已先写库（update_game → _mark_dirty bump
        revision）；否则下次同 revision 的 reload 会用旧 _snapshot 把封面
        回滚（2026-09-10 深度审查 P3-3）。"""
        i = self._row_by_gid.get(gid)
        if i is not None:
            # 只发 dataChanged 不够：self._games[i] 还挂着 reload 时拿的
            # 快照对象，重绘读到的仍是旧封面路径——卡片一直占位图，直到
            # 某次 reload 才恢复（2026-09-09 审查 P1，实测复现）。
            # 取最新记录替换该行（get_game 是 deepcopy，与库契约一致）
            g = self._library.get_game(gid)
            if g is not None:
                self._games[i] = g
                self._tags[i] = sorted({t["source"]
                                        for t in g.get("trainers", [])})
            idx = self.index(i, 0)
            # cover_file 和 cover_url 都可能变化，通知视图两个角色都刷新
            self.dataChanged.emit(idx, idx, [self.Role_CoverUrl,
                                             self.Role_CoverFile])

    def play_stats_changed(self, gid):
        """游玩统计变化（play_count/last_played/play_seconds）后的刷新。
        与 cover_updated 同型：库已更新，但 self._games[i] 还挂着 reload 时
        的快照对象，不发刷新卡片角标会一直陈旧（2026-09-10 深度审查 P2-1）。"""
        if self._source == "最近游玩":
            self.reload()      # 该分类按 last_played 过滤+倒序，需重排
            return
        i = self._row_by_gid.get(gid)
        if i is None:
            return
        g = self._library.get_game(gid)
        if g is not None:
            self._games[i] = g
            self._tags[i] = sorted({t["source"]
                                    for t in g.get("trainers", [])})
        idx = self.index(i, 0)
        self.dataChanged.emit(idx, idx, [self.Role_LastPlayed,
                                         self.Role_PlayCount,
                                         self.Role_PlaySeconds])

    def game_at(self, row):
        return self._games[row] if 0 <= row < len(self._games) else None

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self._games)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._games)):
            return None
        g = self._games[index.row()]
        if role == Qt.DisplayRole or role == self.Role_Name:
            return g["name"]
        if role == self.Role_Running:
            return g["id"] in self._running
        if role == self.Role_GameId:
            return g["id"]
        if role == self.Role_SourceTags:
            return self._tags[index.row()]   # reload 时预建（paint 高频调用）
        if role == self.Role_TrainerCount:
            return len(g.get("trainers", []))
        if role == self.Role_CoverUrl:
            return g.get("cover_url") or ""
        if role == self.Role_CoverFile:
            return g.get("cover_file") or ""
        if role == self.Role_Updatable:
            return g["id"] in self._updatable
        if role == self.Role_LastPlayed:
            return g.get("last_played") or ""
        if role == self.Role_PlayCount:
            return int(g.get("play_count") or 0)
        if role == self.Role_PlaySeconds:
            return int(g.get("play_seconds") or 0)
        if role == Qt.ToolTipRole:
            tip = g["name"]
            lp = g.get("last_played")
            if lp:
                tip += f"\n上次游玩：{lp.replace('T', ' ')}"
            pc = g.get("play_count")
            if pc:
                tip += f"\n累计启动 {pc} 次"
            n_upd = sum(1 for t in g.get("trainers", [])
                        if t.get("update_available"))
            if n_upd:
                tip += f"\n⬆️ {n_upd} 个修改器有新版"
            return tip
        return None


# ---------------------------------------------------------------- 卡片绘制
class CardDelegate(QStyledItemDelegate):
    """性能优化：卡片背景（阴影+圆角+边框）按 4 种状态预渲染为 QPixmap 缓存，
    paint 时一次 drawPixmap；封面预缩放 1:1；每帧仅绘制文字/徽章等轻量元素。

    新手视角：delegate 就是"把一条数据画成什么样"的画笔——数据在模型里，
    这里把每条游戏记录画成一张卡片（封面/名称/来源标签/底部两个按钮），
    具体摆在哪、怎么响应鼠标，由 CardView（QListView）负责。"""

    def __init__(self, covers: CoverLoader, parent=None):
        super().__init__(parent)
        self._covers = covers
        self._name_font = QFont("Microsoft YaHei UI", 9, QFont.DemiBold)
        self._tag_font = QFont("Microsoft YaHei UI", 8)
        self._btn_font = QFont("Microsoft YaHei UI", 9)   # paint 每帧都画按钮，预建
        # 字体度量与字体配对预建：paint 每卡 3 次 QFontMetrics 构造是纯浪费
        self._name_fm = QFontMetrics(self._name_font)
        self._tag_fm = QFontMetrics(self._tag_font)
        self._t = T()                # 主题调色板缓存（retheme 时刷新，paint 不再查）
        self._bg_cache = {}          # (selected, running, hover) -> QPixmap
        self._tag_colors = SRC_COLORS
        # paint 内不得发起封面请求（request 的磁盘快路径会在 GUI 线程做
        # 文件 IO 与同步 emit，导致绘制重入刷新）。未命中的 gid 先收集，
        # 事件循环下一拍批量派发
        self._pending_covers = {}    # gid -> (cover_url, cover_file)
        self._flush_scheduled = False

    def retheme(self):
        """主题切换后刷新缓存的调色板与背景预渲染（与 DetailPanel.retheme 同契约）。"""
        self._t = T()
        self._bg_cache.clear()

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    @staticmethod
    def _paint_btn(painter, rect, text, state, normal, hover, pressed, fg):
        """画卡片底部按钮：state 为 "normal"/"hover"/"pressed"，对应三种配色。

        normal/hover/pressed 是 (R, G, B) 元组，fg 是文字颜色。
        按下时按钮整体下沉 1px 并加深底色，产生“真的按下去”的手感。"""
        if state == "pressed":
            bg = pressed
            rect = rect.translated(0, 1)      # 下沉 1px（底色与文字一起下移）
        elif state == "hover":
            bg = hover                        # 悬停：亮一档，提示可点击
        else:
            bg = normal
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*bg))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(*fg))
        painter.drawText(rect, Qt.AlignCenter, text)

    # ---------- 背景预渲染 ----------
    def _bg_pixmap(self, selected, running, hover):
        key = (selected, running, hover)
        pix = self._bg_cache.get(key)
        if pix is None:
            if len(self._bg_cache) > 16:
                self._bg_cache.clear()
            pix = self._render_bg(selected, running, hover)
            self._bg_cache[key] = pix
        return pix

    def _render_bg(self, selected, running, hover):
        t = self._t
        w, h = CARD_W - 14, CARD_H - 14
        pix = QPixmap(w, h)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRect(0, 0, w, h)
        # 阴影（一次预渲染，颜色随主题）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*t["shadow"]))
        p.drawRoundedRect(rect.translated(0, 3), 12, 12)
        # 背景 + 边框
        if selected:
            bg, border, bw = QColor(t["card_selected_bg"]), QColor(t["accent"]), 2
        elif running:
            bg, border, bw = QColor(t["card_running_bg"]), QColor(t["running"]), 2
        else:
            bg = QColor(t["card_hover"] if hover else t["card"])
            border = QColor(t["accent"] if hover else t["border"])
            bw = 1
        p.setBrush(bg)
        p.setPen(QPen(border, bw))
        p.drawRoundedRect(rect, 12, 12)
        p.end()
        return pix

    def _flush_cover_requests(self):
        """把 paint 期间收集的未命中 gid 批量派发给封面加载器。"""
        self._flush_scheduled = False
        pending, self._pending_covers = self._pending_covers, {}
        for gid, (url, file) in pending.items():
            self._covers.request(gid, url, file)

    def paint(self, painter, option, index):
        painter.save()
        try:
            self._paint(painter, option, index)
        finally:
            # 绘制中途任何异常都不能吃掉 restore，否则 QPainter 状态泄漏
            # 会污染后续所有绘制（裁剪区/画笔错乱）
            painter.restore()

    def _paint(self, painter, option, index):
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = _card_rect(option.rect)

        selected = bool(option.state & QStyle.State_Selected)
        running = bool(index.data(GameListModel.Role_Running))
        hover = bool(option.state & QStyle.State_MouseOver)
        gid = index.data(GameListModel.Role_GameId)
        name = index.data(GameListModel.Role_Name) or ""
        tags = index.data(GameListModel.Role_SourceTags) or []
        cnt = index.data(GameListModel.Role_TrainerCount) or 0

        # ---- 背景：一次 drawPixmap（已含阴影/圆角/边框）
        painter.drawPixmap(rect, self._bg_pixmap(selected, running, hover))

        # ---- 封面：1:1 绘制（预缩放），圆角裁剪
        cover_rect = QRect(rect.left() + 9, rect.top() + 9, rect.width() - 18, 108)
        pix = self._covers.get(gid)
        if pix is None:
            pix = self._covers.placeholder
            # paint 必须是纯绘制：request 的磁盘快路径会在 GUI 线程做文件 IO
            # 并同步 emit 触发视图重入刷新。未命中的 gid 收集起来，
            # 事件循环下一拍批量派发
            if gid not in self._pending_covers:
                self._pending_covers[gid] = (
                    index.data(GameListModel.Role_CoverUrl) or "",
                    index.data(GameListModel.Role_CoverFile) or "")
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    QTimer.singleShot(0, self, self._flush_cover_requests)
        path = QPainterPath()
        path.addRoundedRect(cover_rect, 8, 8)
        painter.setClipPath(path)
        painter.drawPixmap(cover_rect, pix)
        painter.setClipping(False)

        # ---- 封面右下角游玩信息角标（"3天前 · 12次 · 8.2小时"，有记录才显示）
        lp = index.data(GameListModel.Role_LastPlayed)
        if lp:
            rel = _rel_time(lp)
            if rel:
                pc = index.data(GameListModel.Role_PlayCount)
                text = rel + (f" · {pc}次" if pc else "")
                dur = _fmt_duration(index.data(GameListModel.Role_PlaySeconds))
                if dur:
                    text += f" · {dur}"
                painter.setFont(self._tag_font)
                fm = self._tag_fm
                tw = fm.horizontalAdvance(text) + 10
                chip = QRect(cover_rect.right() - tw - 4,
                             cover_rect.bottom() - 18, tw, 15)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0, 150))
                painter.drawRoundedRect(chip, 4, 4)
                painter.setPen(QColor(230, 234, 240))
                painter.drawText(chip, Qt.AlignCenter, text)

        # ---- 名称
        t = self._t
        info_top = cover_rect.bottom() + 7
        painter.setFont(self._name_font)
        painter.setPen(QColor(t["name_fg"]))
        name_rect = QRect(rect.left() + 11, info_top, rect.width() - 22, 20)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         self._name_fm.elidedText(name, Qt.ElideRight,
                                                  name_rect.width()))

        # ---- 来源标签
        painter.setFont(self._tag_font)
        x = rect.left() + 11
        tag_top = info_top + 19
        for tag in tags[:3]:
            painter.setBrush(self._tag_colors.get(tag, QColor(t["tag_fg"])))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPoint(x + 4, tag_top + 8), 3, 3)
            painter.setPen(QColor(t["tag_fg"]))
            painter.drawText(QRect(x + 11, tag_top, 100, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, tag)
            x += 11 + self._tag_fm.horizontalAdvance(tag) + 13

        # ---- 底部双按钮：▶ 启动游戏 / ⚡ 修改器——悬停该卡片才浮现。
        # 常驻按钮（22 游戏 = 44 个实心按钮）视觉噪音大且占卡片高度；
        # 浮现前底部留白（2026-09-10 UI 审查 P2-1）
        hover_btn = None
        pressed_btn = None
        show_btns = False
        view = option.widget
        if isinstance(view, CardView):
            hover_btn = view.hover_button()
            pressed_btn = view.pressed_button()
            show_btns = (view._hover_card == gid
                         or (hover_btn is not None and hover_btn[0] == gid)
                         or (pressed_btn is not None and pressed_btn[0] == gid))
        if show_btns:
            # 浮层：底部渐变加深，按钮从内容上清晰浮起（悬停即聚焦操作）
            veil = QRect(rect.left() + 4, rect.bottom() - 46,
                         rect.width() - 8, 42)
            grad = QLinearGradient(veil.topLeft(), veil.bottomLeft())
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(1.0, QColor(0, 0, 0, 150))
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            painter.setClipPath(path)
            painter.fillRect(veil, grad)
            painter.restore()

            game_btn, trainer_btn = _btn_rects(rect)
            painter.setFont(self._btn_font)

            def btn_state(key):
                if pressed_btn and pressed_btn[0] == gid and pressed_btn[1] == key:
                    return "pressed"
                if hover_btn and hover_btn[0] == gid and hover_btn[1] == key:
                    return "hover"
                return "normal"

            # 启动游戏（蓝）
            self._paint_btn(painter, game_btn, "▶ 启动游戏", btn_state("game"),
                            t["btn_blue_n"], t["btn_blue_h"], t["btn_blue_p"],
                            (255, 255, 255))
            # 修改器（有→绿，无→灰"添加"）
            if cnt:
                self._paint_btn(painter, trainer_btn, f"⚡ 修改器({cnt})",
                                btn_state("trainer"),
                                t["btn_green_n"], t["btn_green_h"], t["btn_green_p"],
                                (255, 255, 255))
            else:
                self._paint_btn(painter, trainer_btn, "＋ 添加修改器",
                                btn_state("trainer"),
                                t["btn_gray_n"], t["btn_gray_h"], t["btn_gray_p"],
                                t["btn_gray_fg"])
            # 可更新角标：修改器按钮右上角橙色点（更新检查发现新版时）
            if index.data(GameListModel.Role_Updatable):
                dot_r = 5
                dot = QRect(trainer_btn.right() - dot_r,
                            trainer_btn.top() - dot_r + 2, dot_r * 2, dot_r * 2)
                painter.setPen(QPen(QColor(t["card"]), 2))
                painter.setBrush(QColor(255, 149, 0))
                painter.drawEllipse(dot)
        elif index.data(GameListModel.Role_Updatable):
            # 非 hover：更新角标改挂封面右上角——按钮隐藏后信息不丢失
            dot_r = 5
            dot = QRect(cover_rect.right() - dot_r * 2 - 4, cover_rect.top() + 4,
                        dot_r * 2, dot_r * 2)
            painter.setPen(QPen(QColor(t["card"]), 2))
            painter.setBrush(QColor(255, 149, 0))
            painter.drawEllipse(dot)

        # ---- 运行中徽章（左上）
        if running:
            chip = QRect(rect.left() + 10, rect.top() + 10, 68, 22)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(*t["running_chip_bg"]))
            painter.drawRoundedRect(chip, 11, 11)
            painter.setPen(QColor(*t["running_chip_fg"]))
            painter.drawEllipse(QRect(chip.left() + 7, chip.top() + 8, 6, 6))
            painter.drawText(QRect(chip.left() + 17, chip.top(), chip.width() - 18,
                                   22), Qt.AlignVCenter, "运行中")


class SidebarDelegate(QStyledItemDelegate):
    """侧边栏条目绘制：名称左对齐 + 计数右对齐（替代空格拼接，杜绝参差）。
    选中：圆角背景 + 左侧强调条 + 加粗；悬停：浅背景。
    Role_Sep 为真时在条目顶部画分隔线（视觉分组：状态 | 来源 | 筛选）。"""
    Role_Count = Qt.UserRole + 1
    Role_Sep = Qt.UserRole + 2

    def sizeHint(self, option, index):
        h = 34 + (6 if index.data(self.Role_Sep) else 0)
        return QSize(option.rect.width(), h)

    def paint(self, painter, option, index):
        painter.save()
        try:
            self._paint(painter, option, index)
        finally:
            painter.restore()

    def _paint(self, painter, option, index):
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = option.rect.adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.State_Selected)
        if index.data(self.Role_Sep):
            painter.setPen(QColor(T()["border"]))
            y = option.rect.top() + 3
            painter.drawLine(option.rect.left() + 12, y,
                             option.rect.right() - 12, y)
        hover = bool(option.state & QStyle.State_MouseOver)

        if selected or hover:
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            t = T()
            painter.fillPath(path, QColor(t["side_selected"] if selected
                                          else t["side_hover"]))
        if selected:
            bar = QRect(rect.left(), rect.top() + 6, 3, rect.height() - 12)
            painter.fillRect(bar, QColor(T()["accent"]))

        name = index.data(Qt.UserRole) or ""
        count = index.data(self.Role_Count)
        name_rect = rect.adjusted(12, 0, -40, 0)
        font = QFont(option.font)
        if selected:
            font.setBold(True)
        painter.setFont(font)
        t = T()
        painter.setPen(QColor(t["text"] if (selected or hover) else t["text_dim"]))
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(
                             name, Qt.ElideRight, name_rect.width()))
        if count is not None:
            painter.setFont(QFont(option.font))
            if count == 0 and not selected:
                # 空分类的 0 计数置灰淡显：白占一行但不再抢注意力
                #（不隐藏，避免选中态切换时布局跳动）
                c = QColor(t["side_count"])
                c.setAlpha(90)
                painter.setPen(c)
            else:
                painter.setPen(QColor(t["side_count_selected"] if selected
                                      else t["side_count"]))
            painter.drawText(rect.adjusted(0, 0, -12, 0),
                             Qt.AlignRight | Qt.AlignVCenter, str(count))


class CardView(QListView):
    """卡片墙视图（View 层）：摆放卡片 + 处理鼠标（点卡片/按钮、右键菜单）。

    新手视角：卡片"长什么样"由 CardDelegate 画，"摆在哪、怎么响应鼠标"由这里管；
    按钮的悬停高亮和按下效果状态也在这里维护（_hover_btn/_pressed_btn），
    绘制时 delegate 通过 hover_button()/pressed_button() 查过来。"""
    cardClicked = Signal(str)
    cardDoubleClicked = Signal(str)
    gameBtnClicked = Signal(str)
    deleteRequested = Signal(str)
    trainerBtnClicked = Signal(str)
    rightClicked = Signal(str, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        # 像素级滚动：滚轮/触摸板跟手（默认逐项跳一卡高，很生硬）
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        # 布局批量计算，滚动时减少重排开销
        self.setLayoutMode(QListView.Batched)
        self.setBatchSize(20)
        self.customContextMenuRequested.connect(self._on_context)
        # 卡片按钮反馈状态：(gid, "game"|"trainer") 或 None。
        # 由鼠标事件维护，delegate 绘制时读取，实现悬停高亮与按下效果
        self._hover_btn = None
        self._pressed_btn = None
        # 当前悬停的卡片 gid：按钮改为"悬停卡片才浮现"（2026-09-10 UI
        # 审查 P2-1），delegate 据此决定是否绘制按钮浮层
        self._hover_card = None

    # ---------- 卡片按钮反馈：悬停高亮 / 按下效果 ----------
    def _button_at(self, pos):
        """pos（视口坐标）落在哪张卡片的按钮上：返回 (gid, "game"|"trainer") 或 None。
        按钮仅在悬停该卡片时显示（浮现式），非悬停卡片不命中，与绘制一致。"""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        gid = idx.data(GameListModel.Role_GameId)
        if gid != self._hover_card:
            return None
        game_btn, trainer_btn = _btn_rects(_card_rect(self.visualRect(idx)))
        if game_btn.contains(pos):
            return (gid, "game")
        if trainer_btn.contains(pos):
            return (gid, "trainer")
        return None

    def hover_button(self):
        """当前悬停的按钮（供 delegate 绘制时查询）。"""
        return self._hover_btn

    def pressed_button(self):
        """当前按住的按钮（供 delegate 绘制时查询）。"""
        return self._pressed_btn

    def mousePressEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid() and e.button() == Qt.LeftButton:
            gid = idx.data(GameListModel.Role_GameId)
            pos = e.position().toPoint()
            # 按下卡片按钮：只记录“按下”并重绘（立刻出现按压视觉），
            # 不在这里触发动作——等松开且仍在该按钮内才算一次有效点击
            # （标准按钮行为：按住看得到反馈，拖出去松开则取消）
            btn = self._button_at(pos)
            if btn in ((gid, "game"), (gid, "trainer")):
                # 命中按钮不走 super() 的默认选中逻辑，手动补选中：否则
                # 键盘 Delete/方向键仍作用于旧选中卡片（2026-09-04 审查 P3）
                if self.currentIndex() != idx:
                    self.setCurrentIndex(idx)
            if btn == (gid, "game"):
                self._pressed_btn = btn
                self._hover_btn = btn
                self.viewport().update()
                e.accept()
                return
            if btn == (gid, "trainer"):
                self._pressed_btn = btn
                self._hover_btn = btn
                self.viewport().update()
                e.accept()
                return
            self.cardClicked.emit(gid)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        # 松开时：若之前按住了卡片按钮，判断松开点是否仍在同一按钮内；
        # 是 → 发出对应信号（gameBtnClicked/trainerBtnClicked），否 → 本次点击取消
        if self._pressed_btn is not None and e.button() == Qt.LeftButton:
            gid, which = self._pressed_btn
            cur = self._button_at(e.position().toPoint())
            self._pressed_btn = None
            self._hover_btn = cur
            self.viewport().update()
            if cur == (gid, which):
                if which == "game":
                    self.gameBtnClicked.emit(gid)
                else:
                    self.trainerBtnClicked.emit(gid)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        card = idx.data(GameListModel.Role_GameId) if idx.isValid() else None
        changed = card != self._hover_card
        if changed:
            self._hover_card = card
        # 悬停目标变化时才重绘（防止每次移动都触发整屏刷新）
        new = self._button_at(pos)
        if new != self._hover_btn:
            self._hover_btn = new
            changed = True
        if changed:
            self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        # 鼠标离开视图：清空按下/悬停状态，防止按钮“卡”在按下外观
        if (self._pressed_btn is not None or self._hover_btn is not None
                or self._hover_card is not None):
            self._pressed_btn = None
            self._hover_btn = None
            self._hover_card = None
            self.viewport().update()
        super().leaveEvent(e)

    def mouseDoubleClickEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid() and e.button() == Qt.LeftButton:
            # 双击落在卡片按钮上：单击路径（press/release）已触发过动作，
            # 再发 cardDoubleClicked 会把游戏启动两次，直接吞掉
            if self._button_at(e.position().toPoint()) is not None:
                e.accept()
                return
            self.cardDoubleClicked.emit(idx.data(GameListModel.Role_GameId))
            return
        # 非左键双击（如右键）不当作"启动游戏"，交给默认实现/已有菜单逻辑
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        """键盘流：Enter 启动游戏 / Ctrl+Enter 启动修改器 / Delete 删除游戏。"""
        idx = self.currentIndex()
        if idx.isValid() and e.key() in (Qt.Key_Return, Qt.Key_Enter):
            gid = idx.data(GameListModel.Role_GameId)
            if e.modifiers() & Qt.ControlModifier:
                self.trainerBtnClicked.emit(gid)
            else:
                self.cardDoubleClicked.emit(gid)
            return
        if idx.isValid() and e.key() == Qt.Key_Delete:
            self.deleteRequested.emit(idx.data(GameListModel.Role_GameId))
            return
        super().keyPressEvent(e)

    def _on_context(self, pos):
        idx = self.indexAt(pos)
        if idx.isValid():
            self.rightClicked.emit(idx.data(GameListModel.Role_GameId),
                                   self.viewport().mapToGlobal(pos))

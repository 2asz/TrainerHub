"""封面生成：exe 图标封面 + 名称首字母兜底封面。

设计要点：
- 图标封面把 exe 大图标按"铺满裁剪"缩放到卡片封面尺寸，解决以前"居中图标四周留空"的问题；
- 提取不到图标时，用游戏名首字母生成一张纯色封面，保证 Steam/Epic/本地游戏都有图可看。
"""
import ctypes
import hashlib
import re
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics, QImage, QPainter, QColor, QLinearGradient

from . import audit
from .config import DATA_DIR
from .install_info import resolve_game_exe

# 封面画布标准尺寸（与 UI 卡片的封面区一致）。
# 单一定义处：卡片布局（ui/main_window.py 的 CARD_W/CARD_H）从这里引用，
# 离线封面生成也用同一值——改尺寸只改这里，两边永不脱节。
COVER_W, COVER_H = 268, 108


# ---------------------------------------------------------------- 图标提取

# 请求尺寸提取失败时逐级降级：很多 3A 游戏的 256px 图标是 PNG 压缩格式，
# GetIconInfo 拿不到色彩位图（Windows 限制），而 ≤128px 的 BMP 格式能正常读取；
# 128px 源放大到 268×108 封面略糊，但远好于退回首字母
_ICON_SIZES = (256, 128, 96, 64, 48, 32)

# ctypes 结构与函数签名只定义一次（原先在 _extract_icon_once 内每次调用
# 都重定义一遍；windll.<dll> 属性访问每次还会新建 WinDLL 实例，一并缓存）
_USER32 = ctypes.windll.user32
_GDI32 = ctypes.windll.gdi32
_SHELL32 = ctypes.windll.shell32


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class _BM(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", wintypes.LPVOID)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_USER32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
_GDI32.DeleteObject.argtypes = [wintypes.HANDLE]
_GDI32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                             wintypes.UINT, ctypes.c_void_p,
                             ctypes.POINTER(_BMIH), wintypes.UINT]
_USER32.DestroyIcon.argtypes = [wintypes.HICON]


def _exe_icon_image(exe_path, size=256):
    """提取 exe 图标为 QImage：按请求尺寸提取，失败逐级降级。
    全程使用 QImage（不用 QPixmap）：QPixmap 只能在 GUI 线程使用，
    本函数会被后台线程调用（离线封面生成），QImage 任意线程安全。"""
    for s in _ICON_SIZES:
        if s > size:
            continue
        img = _extract_icon_once(exe_path, s)
        if img is not None and not img.isNull():
            return img
    return QImage()


def _extract_icon_once(exe_path, size):
    """按指定尺寸提取一次图标；任何一步失败返回空 QImage。"""
    try:
        hicon = wintypes.HICON()
        r = _SHELL32.SHDefExtractIconW(
            str(exe_path), 0, 0, ctypes.byref(hicon), None, size)
        if r == 0 and hicon.value:
            try:
                ii = _ICONINFO()
                if _USER32.GetIconInfo(hicon, ctypes.byref(ii)):
                    try:
                        bm = _BM()
                        if _GDI32.GetObjectW(ii.hbmColor, ctypes.sizeof(_BM),
                                            ctypes.byref(bm)):
                            w, h = bm.bmWidth, bm.bmHeight
                            if w > 0 and h > 0:
                                bmih = _BMIH()
                                bmih.biSize = ctypes.sizeof(_BMIH)
                                bmih.biWidth, bmih.biHeight = w, -h
                                bmih.biPlanes, bmih.biBitCount = 1, 32
                                buf = ctypes.create_string_buffer(w * h * 4)
                                hdc = _USER32.GetDC(None)
                                try:
                                    ok = _GDI32.GetDIBits(hdc, ii.hbmColor, 0, h,
                                                          buf, ctypes.byref(bmih), 0)
                                finally:
                                    _USER32.ReleaseDC(None, hdc)
                                if ok:
                                    img = QImage(bytes(buf), w, h, w * 4,
                                                 QImage.Format_ARGB32).copy()
                                    if not img.isNull():
                                        return img
                    finally:
                        _GDI32.DeleteObject(ii.hbmColor)
                        _GDI32.DeleteObject(ii.hbmMask)
            finally:
                _USER32.DestroyIcon(hicon)
    except Exception as e:
        # 留痕：提取失败常是 GDI 资源/句柄问题，静默会变成"全部游戏都首字母封面"却无从排查
        audit.warning(f"exe 图标提取失败: {type(e).__name__}: {e}")
    # 提取失败返回空 QImage：上层会改用"首字母纯色封面"兜底。
    # 不要用 QFileIconProvider/QPixmap 回退——它们属于 Qt GUI 线程专属 API，
    # 本函数会在后台线程（离线封面 worker）里调用，属于随机崩溃源
    return QImage()


# ---------------------------------------------------------------- 封面保存路径

def _cover_path_for_gid(gid) -> Path:
    """离线封面文件命名：offline-<sha256(gid)前16位>.png。

    为什么加 offline- 前缀：官方封面下载的磁盘缓存是 <sha256(gid)>.png，
    如果离线图标也用同名文件，加载器会命中磁盘缓存而永远不下载官方高清封面；
    用不同的文件名，官方封面才能正常覆盖离线兜底。
    """
    return DATA_DIR / "covers" / ("offline-"
                                  + hashlib.sha256(str(gid).encode("utf-8"))
                                  .hexdigest()[:16] + ".png")


# ---------------------------------------------------------------- exe 图标封面（铺满裁剪）

def make_icon_cover(gid, exe_path, w=COVER_W, h=COVER_H) -> str | None:
    """本地游戏封面兜底（离线，不依赖网络/加速器）：提取 exe 大图标，
    按"铺满裁剪"缩放到封面尺寸，保存为 data/covers/offline-<sha256(gid)>.png。

    与旧的"居中缩放"不同：旧做法把正方形图标缩成 96x96 放在中间，
    卡片封面是 268x108 的宽横幅，四周会露出大量背景；
    新做法使用 KeepAspectRatioByExpanding，让图标至少填满整个画布，
    多出的部分居中裁剪掉，视觉上不再有空白边框。
    失败（文件不存在/无图标/保存失败）返回 None。
    """
    if not exe_path or not Path(exe_path).is_file():
        return None
    try:
        # 256px 源：后续铺满裁剪到 268x108 时更清晰（尤其宽横幅场景）
        icon = _exe_icon_image(exe_path, size=256)
        if icon.isNull():
            return None
        canvas = QImage(w, h, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        try:
            # 深色渐变背景：即使图标带透明边缘也不会突兀
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, QColor("#232b38"))
            grad.setColorAt(1.0, QColor("#141922"))
            p.fillRect(0, 0, w, h, grad)
            # 铺满裁剪：缩放后至少有一边等于画布尺寸，另一边可能超出，居中绘制
            scaled = icon.scaled(w, h, Qt.KeepAspectRatioByExpanding,
                                 Qt.SmoothTransformation)
            x = (w - scaled.width()) // 2
            y = (h - scaled.height()) // 2
            p.drawImage(x, y, scaled)
        finally:
            # 绘制中途异常也要收笔：painter 未 end 时 Qt 析构会告警且设备标脏
            p.end()
        dst = _cover_path_for_gid(gid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        if canvas.save(str(tmp), "PNG"):
            # 原子替换：直接 save 最终路径时中断（被杀/磁盘满）会留下截断
            # PNG，而离线 worker 见"文件存在"就跳过重生成，坏封面永久化
            #（2026-09-10 深度审查 P2-2）
            tmp.replace(dst)
            return str(dst)
    except Exception as e:
        audit.warning(f"封面生成失败: {type(e).__name__}: {e}")
    return None


# ---------------------------------------------------------------- 首字母兜底封面

def _initials(name: str) -> str:
    """从游戏名取首字母，最多两位。

    例如：
    - "God of War" → "GW"
    - "Desert Stalker" → "DS"
    - "Paradox Launcher v2" → "PL"
    纯中文/无字母时取第一个可见字符。
    """
    words = re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", name or "")
    chars = []
    for w in words:
        if not w:
            continue
        # 中文每个词直接取第一个字（大写化不影响中文字符）
        chars.append(w[0].upper())
        if len(chars) >= 2:
            break
    if chars:
        return "".join(chars)
    # 兜底：取第一个非空白字符
    s = (name or "?").strip()
    return s[0].upper() if s else "?"


def make_initial_cover(gid, name, w=COVER_W, h=COVER_H) -> str | None:
    """用游戏名首字母生成纯色封面，作为"实在找不到 exe 图标"时的兜底。

    颜色由游戏名哈希决定，保证同一游戏稳定、不同游戏区分明显；
    保存路径与网络封面/图标封面一致，显示链路零改动。
    """
    try:
        canvas = QImage(w, h, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        try:
            # 色相由名称的稳定哈希决定（不能用内建 hash()——它对字符串按进程
            # 加盐随机，同一游戏的封面颜色会每次重启都变）；
            # 饱和度和明度固定在中等偏暗范围，适配深色主题
            hue = int(hashlib.sha256((name or gid or "?").encode("utf-8"))
                      .hexdigest()[:8], 16) % 360
            base = QColor.fromHsv(hue, 175, 100)
            dark = QColor.fromHsv(hue, 185, 72)
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, base)
            grad.setColorAt(1.0, dark)
            p.fillRect(0, 0, w, h, grad)
            # 首字母：白色大号粗体（浅色描边在深色渐变上足够清晰）
            p.setPen(QColor(240, 242, 245))
            font = QFont("Microsoft YaHei UI", 36, QFont.Bold)
            p.setFont(font)
            fm = QFontMetrics(font)
            text = _initials(name)
            rc = fm.boundingRect(text)
            tx = (w - rc.width()) // 2
            # 基线居中：rect.bottom() 是字串下降后的底线，用 ascent 修正
            ty = (h + fm.ascent()) // 2 - 2
            p.drawText(tx, ty, text)
        finally:
            p.end()
        dst = _cover_path_for_gid(gid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        if canvas.save(str(tmp), "PNG"):
            # 原子替换：直接 save 最终路径时中断（被杀/磁盘满）会留下截断
            # PNG，而离线 worker 见"文件存在"就跳过重生成，坏封面永久化
            #（2026-09-10 深度审查 P2-2）
            tmp.replace(dst)
            return str(dst)
    except Exception as e:
        audit.warning(f"封面生成失败: {type(e).__name__}: {e}")
    return None


# ---------------------------------------------------------------- 统一入口

def generate_cover_for_game(game: dict) -> str | None:
    """为任意游戏生成离线封面（本地/Steam/Epic 都支持）。

    优先提取游戏安装目录里主程序的 exe 图标做铺满封面；
    找不到图标时用游戏名首字母兜底。返回封面文件绝对路径或 None。
    """
    gid = game.get("id")
    if not gid:
        return None
    exe = resolve_game_exe(game)
    if exe:
        cover = make_icon_cover(gid, exe)
        if cover:
            return cover
    return make_initial_cover(gid, game.get("name", ""))

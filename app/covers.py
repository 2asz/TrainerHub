"""封面生成：exe 图标封面 + 名称首字母兜底封面。

设计要点：
- 图标封面把 exe 大图标按"铺满裁剪"缩放到卡片封面尺寸，解决以前"居中图标四周留空"的问题；
- 提取不到图标时，用游戏名首字母生成一张纯色封面，保证 Steam/Epic/本地游戏都有图可看。
"""
import hashlib
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics, QImage, QPainter, QColor, QLinearGradient, QPixmap

from .config import DATA_DIR
from .install_info import resolve_game_exe


# ---------------------------------------------------------------- 图标提取

def _exe_icon_image(exe_path, size=128):
    """提取 exe 大尺寸图标：优先 SHDefExtractIconW（可取 128px 源，清晰），
    失败回退 QFileIconProvider（多为 32/48px 源）。

    全程使用 QImage（不用 QPixmap）：QPixmap 只能在 GUI 线程使用，
    本函数会被后台线程调用（首启离线封面生成），QImage 任意线程安全。
    """
    import ctypes
    from ctypes import wintypes
    try:
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

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetIconInfo.argtypes = [wintypes.HICON,
                                       ctypes.POINTER(_ICONINFO)]
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p,
                                    ctypes.POINTER(_BMIH), wintypes.UINT]
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        hicon = wintypes.HICON()
        r = ctypes.windll.shell32.SHDefExtractIconW(
            str(exe_path), 0, 0, ctypes.byref(hicon), None, size)
        if r == 0 and hicon.value:
            try:
                ii = _ICONINFO()
                if user32.GetIconInfo(hicon, ctypes.byref(ii)):
                    try:
                        bm = _BM()
                        if gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(_BM),
                                            ctypes.byref(bm)):
                            w, h = bm.bmWidth, bm.bmHeight
                            if w > 0 and h > 0:
                                bmih = _BMIH()
                                bmih.biSize = ctypes.sizeof(_BMIH)
                                bmih.biWidth, bmih.biHeight = w, -h
                                bmih.biPlanes, bmih.biBitCount = 1, 32
                                buf = ctypes.create_string_buffer(w * h * 4)
                                hdc = user32.GetDC(None)
                                try:
                                    ok = gdi32.GetDIBits(hdc, ii.hbmColor, 0, h,
                                                         buf, ctypes.byref(bmih), 0)
                                finally:
                                    user32.ReleaseDC(None, hdc)
                                if ok:
                                    img = QImage(bytes(buf), w, h, w * 4,
                                                 QImage.Format_ARGB32).copy()
                                    if not img.isNull():
                                        return img
                    finally:
                        gdi32.DeleteObject(ii.hbmColor)
                        gdi32.DeleteObject(ii.hbmMask)
            finally:
                user32.DestroyIcon(hicon)
    except Exception:
        pass
    # 回退：系统图标提供器（源通常 32/48px），转为 QImage 返回
    from PySide6.QtCore import QFileInfo
    from PySide6.QtWidgets import QFileIconProvider
    pix = QFileIconProvider().icon(QFileInfo(str(exe_path))).pixmap(size, size)
    return pix.toImage() if not pix.isNull() else QImage()


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

def make_icon_cover(gid, exe_path, w=268, h=108) -> str | None:
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
        p.end()
        dst = _cover_path_for_gid(gid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if canvas.save(str(dst), "PNG"):
            return str(dst)
    except Exception:
        pass
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


def make_initial_cover(gid, name, w=268, h=108) -> str | None:
    """用游戏名首字母生成纯色封面，作为"实在找不到 exe 图标"时的兜底。

    颜色由游戏名哈希决定，保证同一游戏稳定、不同游戏区分明显；
    保存路径与网络封面/图标封面一致，显示链路零改动。
    """
    try:
        canvas = QImage(w, h, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        # 用名称哈希确定色相，饱和度和明度固定在中等偏暗范围，适配深色主题
        hue = hash(name or gid) % 360
        base = QColor.fromHsv(hue, 175, 100)
        dark = QColor.fromHsv(hue, 185, 72)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, base)
        grad.setColorAt(1.0, dark)
        p.fillRect(0, 0, w, h, grad)
        # 首字母：白色 + 轻微阴影感（用大号粗体）
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
        p.end()
        dst = _cover_path_for_gid(gid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if canvas.save(str(dst), "PNG"):
            return str(dst)
    except Exception:
        pass
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

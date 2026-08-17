"""读取 Windows exe 文件版本信息（ProductName/FileDescription 等）。
纯 ctypes 调用 version.dll，无第三方依赖，PyInstaller 打包兼容。
失败（非 Windows / 无版本资源）返回 None。"""
import ctypes
import struct
from ctypes import wintypes
from pathlib import Path

try:
    _GetFileVersionInfoSizeW = ctypes.windll.version.GetFileVersionInfoSizeW
    _GetFileVersionInfoW = ctypes.windll.version.GetFileVersionInfoW
    _VerQueryValueW = ctypes.windll.version.VerQueryValueW
    _GetFileVersionInfoSizeW.restype = wintypes.DWORD
    _GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p]
    _VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(wintypes.UINT)]
except (AttributeError, OSError):
    # 非 Windows 平台：所有调用降级
    _GetFileVersionInfoSizeW = _GetFileVersionInfoW = _VerQueryValueW = None

_KEY_ORDER = ("ProductName", "FileDescription", "InternalName", "OriginalFilename")


def get_exe_info(path) -> dict | None:
    """返回 {product_name, file_description}；无元数据返回 None。"""
    if _GetFileVersionInfoSizeW is None:
        return None
    p = str(Path(path))
    size = _GetFileVersionInfoSizeW(p, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not _GetFileVersionInfoW(p, 0, size, buf):
        return None

    def query(subblock):
        """VerQueryValueW 的 pu 参数：对二进制结构（Translation）是字节数，
        对宽字符串（StringFileInfo）是 WCHAR 数量。返回 (内存地址, pu)，单位判断交给调用方。"""
        pp = ctypes.c_void_p()
        pu = wintypes.UINT()
        if _VerQueryValueW(buf, subblock, ctypes.byref(pp), ctypes.byref(pu)):
            return pp.value, pu.value
        return None

    # 取 translation（lang, codepage）拼 StringFileInfo 路径
    got = query(r"\VarFileInfo\Translation")
    if not got or got[1] < 4:
        return None
    trans = ctypes.string_at(got[0], 4)      # Translation 是二进制结构，字节数固定为 4
    lang, cp = struct.unpack_from("<HH", trans, 0)
    base = r"\StringFileInfo\%04x%04x\\" % (lang, cp)

    values = {}
    for key in _KEY_ORDER:
        got = query(base + key)
        if not got:
            continue
        # 宽字符串：pu 是 WCHAR 数量，字节数 = 数量 ×2（此前按字节数取会截断）
        raw = ctypes.string_at(got[0], got[1] * 2)
        try:
            values[key] = raw.decode("utf-16-le", errors="ignore").strip("\x00 \t")
        except Exception:
            continue
    if not values:
        return None
    return {"product_name": values.get("ProductName"),
            "file_description": values.get("FileDescription")}

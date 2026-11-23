"""Fallback previews from Windows itself (the same thumbnails Explorer shows).

Used only for formats we can't decode ourselves, e.g. videos without an
embedded preview, or HEIC.
"""

import ctypes
import threading
from ctypes import wintypes

from PIL import Image

_lock = threading.Lock()


class _GUID(ctypes.Structure):
    _fields_ = [('a', wintypes.DWORD), ('b', wintypes.WORD), ('c', wintypes.WORD), ('d', ctypes.c_ubyte * 8)]


class _SIZE(ctypes.Structure):
    _fields_ = [('cx', ctypes.c_long), ('cy', ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class _BITMAP(ctypes.Structure):
    _fields_ = [('bmType', ctypes.c_long), ('bmWidth', ctypes.c_long), ('bmHeight', ctypes.c_long),
                ('bmWidthBytes', ctypes.c_long), ('bmPlanes', wintypes.WORD), ('bmBitsPixel', wintypes.WORD),
                ('bmBits', ctypes.c_void_p)]


# IShellItemImageFactory {bcc18b79-ba16-442f-80c4-8a59c30c463b}
_IID = _GUID(0xbcc18b79, 0xba16, 0x442f, (ctypes.c_ubyte * 8)(0x80, 0xc4, 0x8a, 0x59, 0xc3, 0x0c, 0x46, 0x3b))
_SIIGBF_BIGGERSIZEOK = 0x1
_SIIGBF_THUMBNAILONLY = 0x8


def get(path, size):
    """PIL image of the Windows thumbnail for `path`, or None."""
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    with _lock:
        ole32.CoInitializeEx(None, 0x2)
        factory = ctypes.c_void_p()
        if shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(_IID), ctypes.byref(factory)) != 0:
            return None
        vtbl = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                           ctypes.POINTER(ctypes.c_void_p))
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p))(vtbl[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        hbm = ctypes.c_void_p()
        try:
            hr = get_image(factory, _SIZE(size, size), _SIIGBF_BIGGERSIZEOK | _SIIGBF_THUMBNAILONLY,
                           ctypes.byref(hbm))
        finally:
            release(factory)
        if hr != 0 or not hbm:
            return None
        try:
            bm = _BITMAP()
            gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
            w, h = bm.bmWidth, bm.bmHeight
            if w <= 0 or h <= 0:
                return None
            hdr = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            dc = user32.GetDC(None)
            try:
                ok = gdi32.GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(hdr), 0)
            finally:
                user32.ReleaseDC(None, dc)
            if not ok:
                return None
            return Image.frombuffer('RGBA', (w, h), buf.raw, 'raw', 'BGRA', 0, 1).convert('RGB')
        finally:
            gdi32.DeleteObject(hbm)

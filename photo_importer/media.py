"""Reading photo files: capture time, orientation, and preview images.

Everything in this module opens files strictly read-only ('rb').
"""

import datetime as dt
import io
import os
import struct

from PIL import Image, ImageOps

JPG_EXT = {'.jpg', '.jpeg'}
RAW_EXT = {'.rw2', '.nef', '.nrw', '.cr2', '.cr3', '.arw', '.dng', '.raf', '.orf', '.pef', '.srw'}
IMG_EXT = {'.png', '.heic', '.heif', '.tif', '.tiff'}
VID_EXT = {'.mp4', '.mov', '.avi', '.mts', '.m4v'}
MEDIA_EXT = JPG_EXT | RAW_EXT | IMG_EXT | VID_EXT

_HEAD = 128 * 1024
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_INT_FMT = {1: 'B', 3: 'H', 4: 'I', 6: 'b', 7: 'B', 8: 'h', 9: 'i', 13: 'I'}


def kind_of(ext):
    ext = ext.lower()
    if ext in JPG_EXT:
        return 'jpg'
    if ext in RAW_EXT:
        return 'raw'
    if ext in VID_EXT:
        return 'video'
    return 'image'


# ---------------------------------------------------------------- TIFF / EXIF

class _Reader:
    """Random-access reads relative to `base`, served from memory when possible."""

    def __init__(self, f, base=0):
        self.f = f
        self.base = base
        f.seek(base)
        self.head = f.read(_HEAD)

    def read(self, off, n):
        if off < 0 or n <= 0:
            return b''
        if off + n <= len(self.head):
            return self.head[off:off + n]
        self.f.seek(self.base + off)
        return self.f.read(n)


def _ifd(r, off, bo):
    raw = r.read(off, 2)
    if len(raw) < 2:
        return {}, 0
    n = struct.unpack(bo + 'H', raw)[0]
    if not 0 < n <= 2000:
        return {}, 0
    data = r.read(off + 2, n * 12 + 4)
    tags = {}
    for i in range(min(n, len(data) // 12)):
        tag, typ, cnt = struct.unpack_from(bo + 'HHI', data, i * 12)
        tags[tag] = (typ, cnt, data[i * 12 + 8:i * 12 + 12])
    nxt = struct.unpack_from(bo + 'I', data, n * 12)[0] if len(data) >= n * 12 + 4 else 0
    return tags, nxt


def _ints(r, bo, entry):
    if not entry:
        return []
    typ, cnt, raw = entry
    fmt = _INT_FMT.get(typ)
    if not fmt or not 0 < cnt <= 4096:
        return []
    size = _TYPE_SIZE[typ] * cnt
    data = raw if size <= 4 else r.read(struct.unpack(bo + 'I', raw)[0], size)
    if len(data) < size:
        return []
    return list(struct.unpack_from(bo + str(cnt) + fmt, data))


def _ascii(r, bo, entry):
    if not entry:
        return ''
    typ, cnt, raw = entry
    if typ != 2 or not 0 < cnt <= 1000:
        return ''
    data = raw[:cnt] if cnt <= 4 else r.read(struct.unpack(bo + 'I', raw)[0], cnt)
    return data.split(b'\0')[0].decode('ascii', 'replace').strip()


def _parse_time(text):
    try:
        t = dt.datetime.strptime(text[:19], '%Y:%m:%d %H:%M:%S')
    except (ValueError, TypeError):
        return None
    return t if t.year >= 1990 else None


def _tiff_info(r):
    """Walk a TIFF structure. Returns capture time, orientation and embedded JPEG previews."""
    info = {'time': None, 'subsec': '', 'orientation': 1, 'previews': []}
    hdr = r.read(0, 8)
    if len(hdr) < 8 or hdr[:2] not in (b'II', b'MM'):
        return info
    bo = '<' if hdr[:2] == b'II' else '>'
    seen = set()
    fallback_time = None

    def previews_from(tags):
        e = tags.get(0x002E)                       # Panasonic JpgFromRaw
        if e and e[1] > 1000:
            info['previews'].append((struct.unpack(bo + 'I', e[2])[0], e[1]))
        off, ln = _ints(r, bo, tags.get(0x0201)), _ints(r, bo, tags.get(0x0202))
        if off and ln and ln[0] > 1000:
            info['previews'].append((off[0], ln[0]))
        comp = _ints(r, bo, tags.get(0x0103))
        if comp and comp[0] in (6, 7):             # JPEG-compressed strip (CR2, DNG previews)
            off, ln = _ints(r, bo, tags.get(0x0111)), _ints(r, bo, tags.get(0x0117))
            if len(off) == 1 and len(ln) == 1 and ln[0] > 1000:
                info['previews'].append((off[0], ln[0]))

    def walk(off, depth):
        nonlocal fallback_time
        chain = 0
        while off and off not in seen and chain < 6:
            seen.add(off)
            chain += 1
            tags, nxt = _ifd(r, off, bo)
            previews_from(tags)
            if depth == 0 and chain == 1:
                o = _ints(r, bo, tags.get(0x0112))
                if o and 1 <= o[0] <= 8:
                    info['orientation'] = o[0]
                fallback_time = _parse_time(_ascii(r, bo, tags.get(0x0132)))
                exif = _ints(r, bo, tags.get(0x8769))
                if exif:
                    etags, _ = _ifd(r, exif[0], bo)
                    info['time'] = (_parse_time(_ascii(r, bo, etags.get(0x9003)))
                                    or _parse_time(_ascii(r, bo, etags.get(0x9004))))
                    info['subsec'] = _ascii(r, bo, etags.get(0x9291))
            if depth < 2:
                for sub in _ints(r, bo, tags.get(0x014A)):
                    walk(sub, depth + 1)
            off = nxt

    walk(struct.unpack(bo + 'I', hdr[4:8])[0], 0)
    if info['time'] is None:
        info['time'] = fallback_time
    return info


def _jpeg_segments(head):
    """Yield (marker, payload_start, payload_end) for the header segments of a JPEG."""
    if head[:2] != b'\xff\xd8':
        return
    pos = 2
    while pos + 4 <= len(head) and head[pos] == 0xFF:
        marker = head[pos + 1]
        if marker in (0xDA, 0xD9):
            return
        seglen = struct.unpack('>H', head[pos + 2:pos + 4])[0]
        yield marker, pos + 4, pos + 2 + seglen
        pos += 2 + seglen


def _jpeg_exif_base(f):
    """Offset of the TIFF block inside a JPEG's EXIF segment, or None."""
    f.seek(0)
    head = f.read(_HEAD)
    for marker, start, _ in _jpeg_segments(head):
        if marker == 0xE1 and head[start:start + 6] == b'Exif\0\0':
            return start + 6
    return None


def _jpeg_mpf_previews(f):
    """Smaller copies of the picture that many cameras append to a JPEG (MPF).

    Returns [(offset, length)]. Reading one of these (about 1 MB) is far
    quicker than decoding the full photo.
    """
    f.seek(0)
    head = f.read(_HEAD)
    out = []
    for marker, start, end in _jpeg_segments(head):
        if marker != 0xE2 or head[start:start + 4] != b'MPF\0':
            continue
        base = start + 4
        seg = head[base:end]
        if seg[:2] not in (b'II', b'MM'):
            break
        bo = '<' if seg[:2] == b'II' else '>'
        try:
            ifd = struct.unpack_from(bo + 'I', seg, 4)[0]
            for i in range(struct.unpack_from(bo + 'H', seg, ifd)[0]):
                tag, _, cnt, val = struct.unpack_from(bo + 'HHII', seg, ifd + 2 + i * 12)
                if tag == 0xB002:                  # list of images in this file
                    for k in range(cnt // 16):
                        _, size, off = struct.unpack_from(bo + 'III', seg, val + k * 16)
                        if off and size > 1000:    # offset 0 is the main picture itself
                            out.append((base + off, size))
        except struct.error:
            pass
        break
    return out


def _raf_jpeg(f):
    """Fujifilm RAF: the header points straight at an embedded full JPEG."""
    f.seek(0)
    head = f.read(100)
    if not head.startswith(b'FUJIFILMCCD-RAW') or len(head) < 92:
        return None
    off, ln = struct.unpack('>II', head[84:92])
    return (off, ln) if ln > 1000 else None


def read_info(path):
    """Capture time / orientation / embedded previews for one file. Never raises."""
    info = {'time': None, 'subsec': '', 'orientation': 1, 'previews': []}
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, 'rb') as f:
            if ext in JPG_EXT:
                base = _jpeg_exif_base(f)
                if base is not None:
                    info = _tiff_info(_Reader(f, base))
                info['previews'] = _jpeg_mpf_previews(f)
            elif ext == '.raf':
                loc = _raf_jpeg(f)
                if loc:
                    f.seek(loc[0])
                    sub = io.BytesIO(f.read(_HEAD))
                    base = _jpeg_exif_base(sub)
                    if base is not None:
                        info = _tiff_info(_Reader(sub, base))
                    info['previews'] = [loc]
            elif ext in RAW_EXT or ext in ('.tif', '.tiff'):
                info = _tiff_info(_Reader(f, 0))
    except OSError:
        pass
    return info


def capture_time(path, mtime=None):
    """(datetime, subsec, came_from_exif). Falls back to the file's modified time."""
    info = read_info(path)
    if info['time']:
        return info['time'], info['subsec'], True
    if mtime is None:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0
    return dt.datetime.fromtimestamp(mtime).replace(microsecond=0), '', False


def _mp4_atom(f, start, end, wanted):
    """Find a child atom; returns (payload_start, payload_end) or None."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return None
        size, name = struct.unpack('>I4s', hdr[:8])
        head = 8
        if size == 1 and len(hdr) == 16:
            size, head = struct.unpack('>Q', hdr[8:16])[0], 16
        elif size == 0:
            size = end - pos
        if size < head:
            return None
        if name == wanted:
            return pos + head, min(pos + size, end)
        pos += size
    return None


def _video_embedded_jpeg(path):
    """Many cameras (Panasonic among them) tuck a preview JPEG into moov/udta."""
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        moov = _mp4_atom(f, 0, size, b'moov')
        udta = moov and _mp4_atom(f, moov[0], moov[1], b'udta')
        if not udta or udta[1] - udta[0] > 16 * 1024 * 1024:
            return None
        f.seek(udta[0])
        blob = f.read(udta[1] - udta[0])
    i = blob.find(b'\xff\xd8\xff')
    if i < 0:
        return None
    im = Image.open(io.BytesIO(blob[i:]))
    im.load()
    return im


# ------------------------------------------------------------------- previews

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _encode(im, size, quality):
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    if im.mode != 'RGB':
        im = im.convert('RGB')
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=quality)
    return out.getvalue()


def _embedded_preview(path, size, must_cover=False):
    """The largest preview JPEG stored inside the file, or None.

    With must_cover, a preview noticeably smaller than `size` is not accepted
    (the caller will decode the full picture instead).
    """
    info = read_info(path)
    with open(path, 'rb') as f:
        for off, ln in sorted(info['previews'], key=lambda p: -p[1]):
            f.seek(off)
            data = f.read(ln)
            if data[:2] != b'\xff\xd8':
                continue
            try:
                im = Image.open(io.BytesIO(data))
                if must_cover and max(im.size) < 0.85 * size:
                    continue
                im.draft('RGB', (size, size))
                im.load()
            except Exception:
                continue
            if info['orientation'] in _TRANSPOSE:
                im = im.transpose(_TRANSPOSE[info['orientation']])
            return im
    return None


def make_preview(path, size=320, quality=82):
    """JPEG bytes of a preview no larger than `size` px, or None if we can't make one."""
    ext = os.path.splitext(path)[1].lower()
    im = None
    try:
        if ext in RAW_EXT:
            im = _embedded_preview(path, size)
        elif ext in VID_EXT:
            im = _video_embedded_jpeg(path)
        elif ext in JPG_EXT:
            im = _embedded_preview(path, size, must_cover=True)
        if im is None and ext not in RAW_EXT and ext not in VID_EXT:
            with open(path, 'rb') as f:
                im = Image.open(f)
                im.draft('RGB', (size, size))
                im.load()
            im = ImageOps.exif_transpose(im)
    except Exception:
        im = None
    if im is None and ext in RAW_EXT:
        try:                                       # some RAWs open as plain TIFFs
            with open(path, 'rb') as f:
                im = Image.open(f)
                im.load()
            im = ImageOps.exif_transpose(im)
        except Exception:
            im = None
    if im is None or max(im.size) < min(size, 300):
        try:
            from . import shellthumb
            alt = shellthumb.get(path, size)
            if alt is not None and (im is None or max(alt.size) > max(im.size)):
                im = alt
        except Exception:
            pass
    if im is None:
        return None
    try:
        return _encode(im, size, quality)
    except Exception:
        return None

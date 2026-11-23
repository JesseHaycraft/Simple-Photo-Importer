"""Scanning the card and the photo library, and working out which files are new.

The card is only ever listed and read. Nothing here writes to it.
"""

import datetime as dt
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import media

SKIP_DIRS = {'$recycle.bin', 'system volume information'}
EVENT_FOLDER = re.compile(r'^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$')
RENAMED = re.compile(r'^(.+)_\d{1,2}$')


class Stopped(Exception):
    pass


def norm(path):
    return os.path.normcase(os.path.abspath(path)).rstrip('\\/')


def is_inside(child, parent):
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p + os.sep)


def walk_media(root, skip=()):
    """Yield (path, name, size, mtime) for every media file under root.

    `skip` is a collection of normalised folder paths to leave out.
    """
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if (e.name.lower() not in SKIP_DIRS and not e.name.startswith('.')
                            and norm(e.path) not in skip):
                        stack.append(e.path)
                elif os.path.splitext(e.name)[1].lower() in media.MEDIA_EXT:
                    st = e.stat()
                    if st.st_size > 0:
                        yield e.path, e.name, st.st_size, st.st_mtime
            except OSError:
                continue


class LibraryIndex:
    def __init__(self, root):
        self.root = root
        self.by_name = {}      # lowercase file name -> [(path, size, mtime)]
        self.by_stem = {}      # lowercase name without extension -> [(path, size, mtime)]
        self.count = 0

    def add(self, path, name, size, mtime):
        rec = (path, size, mtime)
        low = name.lower()
        stem, ext = os.path.splitext(low)
        self.by_name.setdefault(low, []).append(rec)
        self.by_stem.setdefault(stem, []).append(rec)
        # "P1000123_1.JPG" is what a name clash gets renamed to on import, so
        # also remember it under the camera's original name.
        m = RENAMED.match(stem)
        if m:
            self.by_name.setdefault(m.group(1) + ext, []).append(rec)
        self.count += 1

    def event_folders(self):
        """Existing top-level folders named 'YYYY-MM-DD something'."""
        out = []
        try:
            for e in os.scandir(self.root):
                m = EVENT_FOLDER.match(e.name)
                if m and e.is_dir():
                    out.append({'folder': e.name, 'date': m.group(1), 'name': m.group(2) or ''})
        except OSError:
            pass
        out.sort(key=lambda f: f['folder'])
        return out


def index_library(root, progress, skip=()):
    idx = LibraryIndex(root)
    progress('library', 0, 0)
    if os.path.isdir(root):
        for path, name, size, mtime in walk_media(root, skip):
            idx.add(path, name, size, mtime)
            if idx.count % 500 == 0:
                progress('library', idx.count, 0)
    progress('library', idx.count, idx.count)
    return idx


class MetaCache:
    """Remembers capture times between runs so re-scanning a card is quick."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.dirty = False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _key(path, size, mtime):
        return f'{path.lower()}|{size}|{int(mtime)}'

    def time_of(self, path, size, mtime):
        key = self._key(path, size, mtime)
        hit = self.data.get(key)
        if hit:
            return dt.datetime.fromisoformat(hit[0]), hit[1], hit[2]
        t, subsec, exif = media.capture_time(path, mtime)
        self.data[key] = [t.isoformat(), subsec, exif]
        self.dirty = True
        return t, subsec, exif

    def save(self):
        if not self.dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


def ignore_key(name, size, time):
    return f'{name.lower()}|{size}|{time.isoformat()}'


def _clock_offset(files):
    """How far the camera's own clock is from the file timestamps Windows shows.

    Photos carry the camera's clock time inside them; videos often don't, so
    for those we take the file's modified time and shift it by the same
    amount the photos are shifted (typically a daylight-saving hour).
    """
    diffs = sorted((f['time'] - dt.datetime.fromtimestamp(f['mtime'])).total_seconds()
                   for f in files if f['exif'])
    diffs = [d for d in diffs if abs(d) < 26 * 3600]
    if not diffs:
        return dt.timedelta(0)
    return dt.timedelta(seconds=round(diffs[len(diffs) // 2] / 900) * 900)


def scan(source, lib_root, cache, ignored, progress, should_stop=lambda: False, exclude=()):
    """Compare the card against the library.

    Returns (files, index) where each file is a dict with status
    'new', 'imported' or 'ignored'. `exclude` lists library sub-folders
    (relative to the library) that should not count as "already imported".
    """
    skip = {norm(os.path.join(lib_root, x)) for x in exclude if x}
    if is_inside(source, lib_root):
        skip.add(norm(source))          # importing from a folder inside the library itself
    index = index_library(lib_root, progress, skip)
    if should_stop():
        raise Stopped()

    progress('listing', 0, 0)
    found = []
    for rec in walk_media(source):
        found.append(rec)
        if len(found) % 200 == 0:
            progress('listing', len(found), 0)
            if should_stop():
                raise Stopped()

    total = len(found)
    done = 0
    progress('reading', 0, total)

    def read_one(rec):
        path, name, size, mtime = rec
        if should_stop():
            return None
        return cache.time_of(path, size, mtime)

    files = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for rec, res in zip(found, pool.map(read_one, found)):
            if res is None:
                raise Stopped()
            path, name, size, mtime = rec
            t, subsec, exif = res
            stem, ext = os.path.splitext(name)
            files.append({
                'path': path, 'name': name, 'stem': stem, 'ext': ext.lower(),
                'kind': media.kind_of(ext), 'size': size, 'mtime': mtime,
                'time': t, 'subsec': subsec, 'exif': exif,
                'folder': os.path.dirname(path),
            })
            done += 1
            if done % 25 == 0:
                progress('reading', done, total)
    progress('comparing', 0, total)

    offset = _clock_offset(files)
    if offset:
        for f in files:
            if not f['exif']:
                f['time'] += offset

    def same_moment(lib_rec, t):
        lt, _, _ = cache.time_of(*lib_rec)
        return abs((lt - t).total_seconds()) <= 2

    for i, f in enumerate(files):
        f['status'] = 'new'
        f['match'] = ''
        candidates = index.by_name.get(f['name'].lower(), [])
        exact = next((c for c in candidates if c[1] == f['size']), None)
        if exact:
            f['status'], f['match'] = 'imported', exact[0]
        else:
            # Same name but a different size: only "the same photo" if it was
            # taken at the same moment (e.g. it was edited after import).
            # Otherwise it is just the camera's file counter repeating.
            for c in candidates:
                if same_moment(c, f['time']):
                    f['status'], f['match'] = 'imported', c[0]
                    break
        if f['status'] == 'new' and ignore_key(f['name'], f['size'], f['time']) in ignored:
            f['status'] = 'ignored'
        if i % 200 == 0:
            progress('comparing', i, total)
            if should_stop():
                raise Stopped()

    # Same rule as the original script: a JPG whose RAW twin (same name, taken
    # at the same moment) is already in the library counts as imported.
    for f in files:
        if f['status'] != 'new' or f['kind'] != 'jpg':
            continue
        for c in index.by_stem.get(f['stem'].lower(), []):
            if os.path.splitext(c[0])[1].lower() in media.RAW_EXT and same_moment(c, f['time']):
                f['status'], f['match'] = 'imported', c[0]
                break

    cache.save()
    progress('done', total, total)
    return files, index

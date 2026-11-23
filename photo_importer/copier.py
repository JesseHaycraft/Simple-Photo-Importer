"""Copying new photos into the library.

SAFETY RULES for this module (the only one that creates or changes files
outside the app's own settings/cache):

  * Source files are opened read-only and are never moved, renamed or deleted.
  * Every path we write to goes through _guard(), which refuses anything that
    is not inside the library or that is inside the source (card/camera).
  * Existing library files are never overwritten: new files are created with
    exclusive-create and put in place with a rename that fails if the name
    is taken.
"""

import datetime as dt
import os
import re
import shutil

from .library import is_inside

CHUNK = 4 * 1024 * 1024
TEMP_SUFFIX = '.importing'
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ImportError_(Exception):
    pass


def clean_folder_name(text):
    """Make text safe to use as a single Windows folder name."""
    text = _BAD_CHARS.sub(' ', text or '')
    text = re.sub(r'\s+', ' ', text).strip().rstrip('. ')
    return text[:150].rstrip('. ')


class Copier:
    def __init__(self, source_root, lib_root, progress, should_stop):
        self.source_root = source_root
        self.lib_root = lib_root
        self.progress = progress
        self.should_stop = should_stop

    def _guard(self, path):
        """Refuse to touch anything outside the library or inside the source."""
        if not is_inside(path, self.lib_root):
            raise ImportError_(f'Refusing to write outside the photo library: {path}')
        if is_inside(path, self.source_root):
            raise ImportError_(f'Refusing to write to the card/camera: {path}')
        return path

    def _free_name(self, folder, name):
        target = os.path.join(folder, name)
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(target):
            target = os.path.join(folder, f'{stem}_{n}{ext}')
            n += 1
        return target

    def _copy_one(self, src, size, mtime, dest_dir, name, on_bytes):
        """Returns ('copied'|'exists', final_path)."""
        target = os.path.join(dest_dir, name)
        if os.path.exists(target) and os.path.getsize(target) == size:
            return 'exists', target
        target = self._guard(self._free_name(dest_dir, name))
        tmp = self._guard(target + TEMP_SUFFIX)
        if os.path.exists(tmp):
            os.remove(tmp)                       # leftover of our own from an interrupted run
        try:
            written = 0
            with open(src, 'rb') as fin, open(tmp, 'xb') as fout:
                while True:
                    if self.should_stop():
                        raise InterruptedError()
                    chunk = fin.read(CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    written += len(chunk)
                    on_bytes(len(chunk))
                fout.flush()
                os.fsync(fout.fileno())
            if written != size or os.path.getsize(tmp) != size:
                raise ImportError_(f'Copy of {name} is incomplete ({written} of {size} bytes)')
            os.utime(tmp, (mtime, mtime))        # keep the camera's timestamp on the copy
            os.rename(tmp, target)               # fails rather than overwrite
        except BaseException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        return 'copied', target

    def run(self, jobs, log_path=None):
        """jobs: list of (file_dict, dest_dir). Returns a summary dict."""
        if is_inside(self.lib_root, self.source_root):
            raise ImportError_('The photo library is on the card/camera itself. '
                               'Choose a library folder on your computer.')
        total_bytes = sum(f['size'] for f, _ in jobs)
        free = shutil.disk_usage(_existing_parent(self.lib_root)).free
        if total_bytes > free - 200 * 1024 * 1024:
            raise ImportError_(f'Not enough free space: need {total_bytes / 1e9:.1f} GB, '
                               f'only {free / 1e9:.1f} GB free.')

        summary = {'copied': 0, 'existed': 0, 'failed': [], 'bytes': 0, 'cancelled': False,
                   'folders': {}, 'total': len(jobs), 'log': log_path or ''}
        done_bytes = 0
        log = None
        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log = open(log_path, 'w', encoding='utf-8')
            log.write(f'Photo import {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n'
                      f'From: {self.source_root}\nTo:   {self.lib_root}\n\n')

        def on_bytes(n):
            nonlocal done_bytes
            done_bytes += n
            self.progress('copying', done_bytes, total_bytes, current)

        current = ''
        try:
            for i, (f, dest_dir) in enumerate(jobs):
                if self.should_stop():
                    summary['cancelled'] = True
                    break
                current = f'{i + 1} of {len(jobs)}: {f["name"]}'
                before = done_bytes
                try:
                    os.makedirs(self._guard(dest_dir), exist_ok=True)
                    result, final = self._copy_one(f['path'], f['size'], f['mtime'], dest_dir,
                                                   f['name'], on_bytes)
                except InterruptedError:
                    summary['cancelled'] = True
                    break
                except Exception as e:           # keep going with the rest
                    summary['failed'].append({'name': f['name'], 'error': str(e)})
                    if log:
                        log.write(f'FAILED  {f["path"]}  ({e})\n')
                    done_bytes = before + f['size']
                    continue
                done_bytes = before + f['size']
                self.progress('copying', done_bytes, total_bytes, current)
                if result == 'copied':
                    summary['copied'] += 1
                    summary['bytes'] += f['size']
                else:
                    summary['existed'] += 1
                summary['folders'][dest_dir] = summary['folders'].get(dest_dir, 0) + 1
                if log:
                    log.write(f'{"COPIED " if result == "copied" else "EXISTED"} {f["path"]}  ->  {final}\n')
        finally:
            if log:
                log.write(f'\nCopied {summary["copied"]}, already there {summary["existed"]}, '
                          f'failed {len(summary["failed"])}'
                          f'{", CANCELLED before finishing" if summary["cancelled"] else ""}\n')
                log.close()
        summary['folders'] = [{'path': p, 'count': n} for p, n in summary['folders'].items()]
        return summary


def _existing_parent(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path

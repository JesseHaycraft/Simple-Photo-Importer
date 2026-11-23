"""Local web server behind the Photo Importer window.

Listens on 127.0.0.1 only. The window is an Edge "app" window pointed at it.
"""

import ctypes
import datetime as dt
import hashlib
import json
import os
import string
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import library, media
from .copier import Copier, ImportError_, clean_folder_name

PORT = int(os.environ.get('PORT') or 47815)
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(PKG_DIR)
WEB_DIR = os.path.join(PKG_DIR, 'web')
DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'PhotoImporter')
THUMB_DIR = os.path.join(DATA_DIR, 'thumbs')
SETTINGS_PATH = os.path.join(APP_DIR, 'settings.json')
IGNORED_PATH = os.path.join(APP_DIR, 'ignored.json')
LOG_DIR = os.path.join(APP_DIR, 'logs')

DEFAULT_LIBRARY = os.path.join(os.path.expanduser('~'), 'Pictures', 'Photos life')   # until one is chosen
KIND_ORDER = {'raw': 0, 'jpg': 1, 'image': 2, 'video': 3}
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}

PICKER = r'''
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes('-topmost', True)
path = filedialog.askdirectory(parent=root, title=sys.argv[1], initialdir=sys.argv[2] or None, mustexist=True)
sys.stdout.buffer.write((path or '').encode('utf-8'))
'''


def _load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def list_sources():
    """Drives that look like a camera card (have a DCIM folder)."""
    out = []
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)                               # no "insert a disk" pop-ups
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f'{letter}:\\'
        if k32.GetDriveTypeW(root) not in (2, 3) or not os.path.isdir(root + 'DCIM'):
            continue
        label = ctypes.create_unicode_buffer(261)
        k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
        out.append({'path': root, 'label': f'{label.value or "Card"} ({letter}:)'})
    return out


class App:
    def __init__(self, auto_exit=True):
        self.auto_exit = auto_exit
        self.lock = threading.RLock()
        self.settings = {'library': DEFAULT_LIBRARY, 'subfolder': 'RAW', 'exclude': [], 'source': ''}
        saved = _load_json(SETTINGS_PATH, {})
        self.settings.update({k: saved[k] for k in self.settings if k in saved})
        self.ignored = set(_load_json(IGNORED_PATH, []))
        self.cache = library.MetaCache(os.path.join(DATA_DIR, 'meta.json'))
        self.scan = None
        self.job = {'type': '', 'state': 'idle'}
        self.stop = threading.Event()
        self.thumb_slots = threading.Semaphore(6)
        self.clients = 0
        self.had_client = False
        self.last_seen = time.time()

    # ------------------------------------------------------------ settings

    def config(self):
        s = self.settings
        return {'settings': s, 'sources': list_sources(), 'libraryExists': os.path.isdir(s['library']),
                'job': self.job_status()}

    def update_settings(self, body):
        with self.lock:
            if isinstance(body.get('library'), str) and os.path.isabs(body['library']):
                self.settings['library'] = os.path.normpath(body['library'])
            if isinstance(body.get('subfolder'), str):
                self.settings['subfolder'] = clean_folder_name(body['subfolder'])
            if isinstance(body.get('exclude'), list):
                self.settings['exclude'] = sorted({str(x) for x in body['exclude'] if x})
            if isinstance(body.get('source'), str):
                self.settings['source'] = body['source']
            _save_json(SETTINGS_PATH, self.settings)

    # ---------------------------------------------------------------- jobs

    def job_status(self):
        with self.lock:
            return dict(self.job)

    def _begin(self, kind):
        with self.lock:
            if self.job.get('state') == 'running':
                raise ValueError('Still busy with the previous step.')
            self.stop.clear()
            self.job = {'type': kind, 'state': 'running', 'phase': '', 'done': 0, 'total': 0, 'detail': ''}

    def _progress(self, phase, done, total, detail=''):
        self.job.update(phase=phase, done=done, total=total, detail=detail)

    def _run(self, fn):
        def wrapper():
            try:
                result = fn()
                with self.lock:
                    self.job.update(state='done', result=result)
            except library.Stopped:
                with self.lock:
                    self.job.update(state='error', error='Stopped.')
            except (ImportError_, ValueError) as e:
                with self.lock:
                    self.job.update(state='error', error=str(e))
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.job.update(state='error', error=f'Unexpected problem: {e}')
        threading.Thread(target=wrapper, daemon=True).start()

    # ---------------------------------------------------------------- scan

    def start_scan(self, source):
        if not source or not os.path.isdir(source):
            raise ValueError(f'Cannot open "{source}". Is the card still plugged in?')
        lib_root = self.settings['library']
        self._begin('scan')
        self.update_settings({'source': source})

        def work():
            files, index = library.scan(source, lib_root, self.cache, self.ignored, self._progress,
                                        self.stop.is_set, self.settings['exclude'])
            payload = self._build_payload(files, index, source, lib_root)
            with self.lock:
                self.scan = {'id': payload['scanId'], 'source': source, 'library': lib_root,
                             'files': files, 'payload': payload}
            return {'scanId': payload['scanId']}
        self._run(work)

    def _build_payload(self, files, index, source, lib_root):
        for i, f in enumerate(files):
            f['id'] = i
            f['siblings'] = []
        groups = {}
        for f in files:
            if f['status'] == 'new':
                groups.setdefault((f['folder'].lower(), f['stem'].lower()), []).append(f)
        shots = []
        for group in groups.values():
            group.sort(key=lambda f: (KIND_ORDER[f['kind']], f['name']))
            for f in group:
                f['siblings'] = [g['id'] for g in group if g is not f]
            shots.append({
                'name': group[0]['stem'],
                't': min(f['time'] for f in group).isoformat(),
                'sub': group[0]['subsec'],
                'thumb': group[0]['id'],
                'files': [{'id': f['id'], 'name': f['name'], 'kind': f['kind'], 'size': f['size']} for f in group],
            })
        shots.sort(key=lambda s: (s['t'], s['sub'], s['name']))
        for i, s in enumerate(shots):
            s['id'] = i

        where = {}
        for f in files:
            if f['status'] == 'imported':
                rel = os.path.relpath(f['match'], lib_root).split(os.sep)
                top = rel[0] if len(rel) > 1 else ''
                where[top] = where.get(top, 0) + 1
        ignored = [f for f in files if f['status'] == 'ignored']
        return {
            'scanId': int(time.time() * 1000),
            'source': source, 'library': lib_root,
            'libraryExists': os.path.isdir(lib_root),
            'libraryFiles': index.count,
            'cardFiles': len(files),
            'importedFiles': sum(1 for f in files if f['status'] == 'imported'),
            'ignoredFiles': len(ignored),
            'newFiles': sum(len(s['files']) for s in shots),
            'importedWhere': sorted(({'folder': k, 'count': v} for k, v in where.items()),
                                    key=lambda x: -x['count']),
            'folders': index.event_folders(),
            'shots': shots,
        }

    # -------------------------------------------------------------- import

    def start_import(self, body):
        with self.lock:
            scan = self.scan
        if not scan or body.get('scanId') != scan['id']:
            raise ValueError('The card was re-scanned since this plan was made. Please review it again.')
        if not os.path.isdir(scan['source']):
            raise ValueError('The card is no longer available.')
        files = scan['files']
        sub = clean_folder_name(body.get('subfolder') or '')
        jobs, seen = [], set()
        for ev in body.get('events') or []:
            folder = clean_folder_name(ev.get('folder') or '')
            if not folder:
                raise ValueError('An event has no folder name.')
            dest = os.path.join(scan['library'], folder, sub) if sub else os.path.join(scan['library'], folder)
            for fid in ev.get('files') or []:
                if not isinstance(fid, int) or not 0 <= fid < len(files) or fid in seen:
                    raise ValueError('The import plan does not match the scanned card.')
                seen.add(fid)
                if files[fid]['status'] == 'new':
                    jobs.append((files[fid], dest))
        to_ignore = [files[i] for i in body.get('ignore') or []
                     if isinstance(i, int) and 0 <= i < len(files) and i not in seen]
        if not jobs and not to_ignore:
            raise ValueError('Nothing was selected to import.')
        self._begin('import')
        log_path = os.path.join(LOG_DIR, f'import {dt.datetime.now():%Y-%m-%d %H%M%S}.txt')

        def work():
            summary = {'copied': 0, 'existed': 0, 'failed': [], 'bytes': 0, 'cancelled': False,
                       'folders': [], 'total': 0, 'log': ''}
            if jobs:
                copier = Copier(scan['source'], scan['library'], self._progress, self.stop.is_set)
                summary = copier.run(jobs, log_path)
            if to_ignore and not summary['cancelled']:
                with self.lock:
                    for f in to_ignore:
                        self.ignored.add(library.ignore_key(f['name'], f['size'], f['time']))
                    _save_json(IGNORED_PATH, sorted(self.ignored))
            summary['ignored'] = len(to_ignore)
            summary['library'] = scan['library']
            return summary
        self._run(work)

    def forget_ignored(self):
        with self.lock:
            scan = self.scan
            if scan:
                for f in scan['files']:
                    if f['status'] == 'ignored':
                        self.ignored.discard(library.ignore_key(f['name'], f['size'], f['time']))
                _save_json(IGNORED_PATH, sorted(self.ignored))

    # -------------------------------------------------------------- images

    def preview(self, scan_id, fid, size):
        with self.lock:
            scan = self.scan
        if not scan or scan['id'] != scan_id or not 0 <= fid < len(scan['files']):
            return None
        f = scan['files'][fid]
        key = hashlib.sha1(f'{f["path"].lower()}|{f["size"]}|{int(f["mtime"])}|{size}'.encode()).hexdigest()
        cached = os.path.join(THUMB_DIR, key[:2], key + '.jpg')
        try:
            with open(cached, 'rb') as fh:
                return fh.read()
        except OSError:
            pass
        data = None
        with self.thumb_slots:
            for c in [f] + [scan['files'][i] for i in f['siblings']]:
                data = media.make_preview(c['path'], size, 82 if size <= 400 else 88)
                if data:
                    break
        if data:
            try:
                os.makedirs(os.path.dirname(cached), exist_ok=True)
                tmp = f'{cached}.{threading.get_ident()}.tmp'
                with open(tmp, 'wb') as fh:
                    fh.write(data)
                os.replace(tmp, cached)
            except OSError:
                pass
        return data

    # ----------------------------------------------------------- lifecycle

    def watchdog(self):
        """Quit once the window has been closed."""
        while True:
            time.sleep(1)
            if self.clients > 0:
                self.last_seen = time.time()
                continue
            if not self.auto_exit:
                continue
            limit = 5 if self.had_client else 90
            if time.time() - self.last_seen > limit:
                self.stop.set()                       # stops a copy cleanly, between chunks
                for _ in range(100):
                    if self.job.get('state') != 'running':
                        break
                    time.sleep(0.1)
                self.cache.save()
                os._exit(0)


def prune_thumbs(days=45):
    cutoff = time.time() - days * 86400
    try:
        for folder in os.scandir(THUMB_DIR):
            if folder.is_dir():
                for e in os.scandir(folder.path):
                    if e.stat().st_mtime < cutoff:
                        os.remove(e.path)
    except OSError:
        pass


def pick_folder(title, initial):
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        res = subprocess.run([sys.executable, '-c', PICKER, title, initial or ''],
                             capture_output=True, timeout=900, creationflags=flags)
        return os.path.normpath(res.stdout.decode('utf-8').strip()) if res.stdout.strip() else ''
    except Exception:
        return ''


def make_handler(app):
    hosts = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _send(self, code, body=b'', ctype='application/json', cache='no-store'):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode('utf-8'))

        def do_GET(self):
            if self.headers.get('Host') not in hosts:
                return self._send(403)
            path = self.path.split('?')[0]
            try:
                if path in STATIC:
                    name, ctype = STATIC[path]
                    with open(os.path.join(WEB_DIR, name), 'rb') as f:
                        return self._send(200, f.read(), ctype)
                if path == '/api/config':
                    return self._json(app.config())
                if path == '/api/status':
                    return self._json(app.job_status())
                if path == '/api/scan-result':
                    with app.lock:
                        payload = app.scan and app.scan['payload']
                    return self._json(payload or {})
                if path.startswith(('/thumb/', '/preview/')):
                    _, kind, scan_id, fid = path.split('/')
                    data = app.preview(int(scan_id), int(fid), 320 if kind == 'thumb' else 1800)
                    if data:
                        return self._send(200, data, 'image/jpeg', 'private, max-age=86400')
                    return self._send(404)
                if path == '/api/alive':
                    return self._alive()
                self._send(404)
            except (BrokenPipeError, ConnectionError):
                pass
            except Exception as e:
                traceback.print_exc()
                try:
                    self._json({'error': str(e)}, 500)
                except OSError:
                    pass

        def _alive(self):
            """Held open by the window; when it drops, the window is gone."""
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            with app.lock:
                app.clients += 1
                app.had_client = True
            try:
                while True:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
                    time.sleep(2)
            except OSError:
                pass
            finally:
                with app.lock:
                    app.clients -= 1
                    app.last_seen = time.time()

        def do_POST(self):
            if self.headers.get('Host') not in hosts or self.headers.get('X-Photo-Importer') != '1':
                return self._send(403)
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                path = self.path
                if path == '/api/config':
                    app.update_settings(body)
                    return self._json(app.config())
                if path == '/api/scan':
                    app.start_scan(body.get('source') or '')
                    return self._json({'ok': True})
                if path == '/api/import':
                    app.start_import(body)
                    return self._json({'ok': True})
                if path == '/api/cancel':
                    app.stop.set()
                    return self._json({'ok': True})
                if path == '/api/forget-ignored':
                    app.forget_ignored()
                    return self._json({'ok': True})
                if path == '/api/pick-folder':
                    return self._json({'path': pick_folder(str(body.get('title') or 'Choose a folder'),
                                                           str(body.get('initial') or ''))})
                if path == '/api/open':
                    target = str(body.get('path') or '')
                    if os.path.exists(target) and (library.is_inside(target, app.settings['library'])
                                                   or library.is_inside(target, LOG_DIR)):
                        os.startfile(target)
                        return self._json({'ok': True})
                    return self._json({'error': 'That folder does not exist.'}, 400)
                self._send(404)
            except ValueError as e:
                self._json({'error': str(e)}, 400)
            except (BrokenPipeError, ConnectionError):
                pass
            except Exception as e:
                traceback.print_exc()
                try:
                    self._json({'error': str(e)}, 500)
                except OSError:
                    pass

    return Handler


def open_window(url):
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    for exe in (os.path.join(pf86, r'Microsoft\Edge\Application\msedge.exe'),
                os.path.join(pf, r'Microsoft\Edge\Application\msedge.exe'),
                os.path.join(pf, r'Google\Chrome\Application\chrome.exe'),
                os.path.join(pf86, r'Google\Chrome\Application\chrome.exe')):
        if os.path.exists(exe):
            subprocess.Popen([exe, f'--app={url}', '--window-size=1500,950'])
            return
    webbrowser.open(url)


def main():
    # --no-window: don't open a window.  --stay: keep running after the window closes.
    no_window = '--no-window' in sys.argv
    url = f'http://127.0.0.1:{PORT}/'
    os.makedirs(DATA_DIR, exist_ok=True)
    if sys.stderr is None:                            # started without a console (pythonw)
        sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, 'app.log'), 'w', encoding='utf-8', buffering=1)
    app = App(auto_exit='--stay' not in sys.argv)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 128      # the window asks for many thumbnails at once
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', PORT), make_handler(app))
    except OSError:
        # Already running: just bring up another window onto it.
        if not no_window:
            open_window(url)
        return
    threading.Thread(target=prune_thumbs, daemon=True).start()
    threading.Thread(target=app.watchdog, daemon=True).start()
    if not no_window:
        open_window(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    app.cache.save()

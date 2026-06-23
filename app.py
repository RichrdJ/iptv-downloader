#!/usr/bin/env python3
"""IPTV Series Downloader — Flask web UI"""

import os
import re
import json
import threading
import uuid
import subprocess
from pathlib import Path

import requests as http
from flask import (Flask, render_template, request,
                   redirect, url_for, session, jsonify)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

CONFIG_DIR  = Path(os.environ.get('CONFIG_DIR',  '/config'))
DOWNLOAD_DIR = Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))
CONFIG_FILE  = CONFIG_DIR / 'accounts.json'

CONNECTION_TIMEOUT = 10
READ_TIMEOUT       = 30

_dl_lock = threading.Lock()
_downloads: dict = {}   # job_id -> {...}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return re.sub(r'\s+', ' ', name).strip() or 'Unknown'


def episode_filename(show: str, season: int, ep: int, title: str, ext: str) -> str:
    title_part = f' - {sanitize(title)}' if title and title.strip() else ''
    return f'{sanitize(show)} - S{season:02d}E{ep:02d}{title_part}.{ext}'


def parse_m3u_url(url: str):
    m = re.match(r'(https?://[^/]+)/get\.php\?username=([^&]+)&password=([^&]+)', url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


# ---------------------------------------------------------------------------
# Xtream client
# ---------------------------------------------------------------------------

class XtreamClient:
    def __init__(self, server, username, password):
        self.server   = server.rstrip('/')
        self.username = username
        self.password = password
        self._base    = {'username': username, 'password': password}

    def _get(self, action, extra=None):
        params = {**self._base}
        if action:
            params['action'] = action
        if extra:
            params.update(extra)
        r = http.get(
            f'{self.server}/player_api.php', params=params,
            timeout=(CONNECTION_TIMEOUT, READ_TIMEOUT),
            headers={'User-Agent': 'IPTV-Downloader/1.0'}
        )
        r.raise_for_status()
        return r.json()

    def authenticate(self):
        try:
            return self._get('').get('user_info', {}).get('auth', 0) == 1
        except Exception:
            return False

    def categories(self):
        return self._get('get_series_categories') or []

    def series(self, cat_id=None):
        extra = {'category_id': cat_id} if cat_id else None
        return self._get('get_series', extra) or []

    def series_info(self, series_id):
        return self._get('get_series_info', {'series_id': series_id}) or {}

    def stream_url(self, ep_id, ext):
        return f'{self.server}/series/{self.username}/{self.password}/{ep_id}.{ext}'


def get_client():
    if 'server' not in session:
        return None
    return XtreamClient(session['server'], session['username'], session['password'])


# ---------------------------------------------------------------------------
# Account storage
# ---------------------------------------------------------------------------

def load_accounts():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return []


def save_account(acc):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    accounts = load_accounts()
    accounts.append(acc)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)


def delete_account(idx):
    accounts = load_accounts()
    if 0 <= idx < len(accounts):
        accounts.pop(idx)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(accounts, f, indent=2)


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------

def _worker(job_id, client, episodes, show_name, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    with _dl_lock:
        _downloads[job_id].update(total=len(episodes), done=0, failed=0,
                                   status='running', current=None)

    for ep in episodes:
        with _dl_lock:
            if _downloads[job_id].get('cancelled'):
                break

        season = int(ep.get('season', 1))
        ep_num = int(ep.get('episode_num', 1))
        title  = ep.get('title', '') or ''
        ext    = ep.get('container_extension', 'mkv')
        ep_id  = ep.get('id')

        filename = episode_filename(show_name, season, ep_num, title, ext)
        filepath = out_dir / filename

        with _dl_lock:
            _downloads[job_id]['current'] = filename

        if filepath.exists():
            with _dl_lock:
                _downloads[job_id]['done'] += 1
            continue

        url = client.stream_url(ep_id, ext)
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
               '-i', url, '-c', 'copy', '-y', str(filepath)]

        result = subprocess.run(cmd, capture_output=True, text=True)

        with _dl_lock:
            if result.returncode == 0:
                _downloads[job_id]['done'] += 1
            else:
                _downloads[job_id]['failed'] += 1
                if filepath.exists():
                    filepath.unlink()

    with _dl_lock:
        _downloads[job_id]['status']  = 'done'
        _downloads[job_id]['current'] = None


# ---------------------------------------------------------------------------
# Routes — auth
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('login.html',
                           accounts=load_accounts(),
                           show_manual=request.args.get('manual') == '1',
                           error=None)


@app.route('/connect', methods=['POST'])
def connect():
    action = request.form.get('action')

    if action == 'select':
        idx = int(request.form.get('account_idx', 0))
        accs = load_accounts()
        if 0 <= idx < len(accs):
            a = accs[idx]
            session.update(server=a['server'], username=a['username'],
                           password=a['password'], account_name=a['name'])
        return redirect(url_for('browse'))

    if action == 'delete':
        delete_account(int(request.form.get('account_idx', 0)))
        return redirect(url_for('index'))

    # action == 'login'
    raw = request.form.get('m3u_url', '').strip()
    manual = request.form.get('manual') == '1'

    if manual or not raw.startswith('http') or 'get.php' not in raw:
        server   = raw
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not server or not username:
            return render_template('login.html', accounts=load_accounts(),
                                   show_manual=True,
                                   error='Server URL and username are required.')
    else:
        parsed = parse_m3u_url(raw)
        if not parsed:
            return render_template('login.html', accounts=load_accounts(),
                                   show_manual=False,
                                   error='Invalid M3U+ URL. Expected: http://host/get.php?username=X&password=Y')
        server, username, password = parsed

    client = XtreamClient(server, username, password)
    if not client.authenticate():
        return render_template('login.html', accounts=load_accounts(),
                               show_manual=manual or 'get.php' not in raw,
                               error='Authentication failed — check your credentials.')

    session.update(server=server, username=username, password=password,
                   account_name=f'{username}@{server}')

    if request.form.get('save_account'):
        name = request.form.get('account_name') or session['account_name']
        save_account({'name': name, 'server': server,
                      'username': username, 'password': password})

    return redirect(url_for('browse'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Routes — browse
# ---------------------------------------------------------------------------

@app.route('/browse')
def browse():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    try:
        cats = client.categories()
        error = None
    except Exception as e:
        cats, error = [], str(e)
    return render_template('browse.html', categories=cats, error=error)


@app.route('/category/<cat_id>')
def category(cat_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    try:
        series_list = client.series(cat_id)
        cat_name    = request.args.get('name', 'Category')
        error = None
    except Exception as e:
        series_list, cat_name, error = [], 'Error', str(e)
    return render_template('series_list.html', series_list=series_list,
                           title=cat_name, error=error)


@app.route('/search')
def search():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    q = request.args.get('q', '').strip().lower()
    if not q:
        return redirect(url_for('browse'))
    try:
        results = [s for s in client.series() if q in s.get('name', '').lower()]
        error = None
    except Exception as e:
        results, error = [], str(e)
    return render_template('series_list.html', series_list=results,
                           title=f'Search: {q}', error=error)


@app.route('/series/<int:series_id>')
def series(series_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    try:
        info      = client.series_info(series_id)
        show_name = info.get('info', {}).get('name', 'Unknown')
        eps_by_s  = info.get('episodes', {})
        seasons   = sorted(eps_by_s.keys(), key=lambda x: int(x))
        season_info = [(s, len(eps_by_s[s])) for s in seasons]
        error = None
    except Exception as e:
        show_name, season_info, error = 'Error', [], str(e)
    return render_template('seasons.html', series_name=show_name,
                           season_info=season_info, series_id=series_id, error=error)


# ---------------------------------------------------------------------------
# Routes — download
# ---------------------------------------------------------------------------

@app.route('/download', methods=['POST'])
def start_download():
    client = get_client()
    if not client:
        return redirect(url_for('index'))

    series_id  = int(request.form.get('series_id'))
    season_key = request.form.get('season')   # '1', '2', … or 'all'

    try:
        info      = client.series_info(series_id)
        show_name = info.get('info', {}).get('name', 'Unknown')
        eps_by_s  = info.get('episodes', {})

        if season_key == 'all':
            episodes = []
            for k in sorted(eps_by_s.keys(), key=lambda x: int(x)):
                episodes.extend(eps_by_s[k])
        else:
            episodes = eps_by_s.get(season_key, [])
    except Exception:
        return redirect(url_for('series', series_id=series_id))

    out_dir  = DOWNLOAD_DIR / sanitize(show_name)
    job_id   = str(uuid.uuid4())[:8]
    label    = f"{show_name} — {'All seasons' if season_key == 'all' else f'Season {season_key}'}"

    with _dl_lock:
        _downloads[job_id] = {'label': label, 'total': len(episodes),
                               'done': 0, 'failed': 0, 'status': 'starting', 'current': None}

    threading.Thread(target=_worker,
                     args=(job_id, client, episodes, show_name, out_dir),
                     daemon=True).start()

    return redirect(url_for('downloads'))


@app.route('/downloads')
def downloads():
    return render_template('downloads.html')


@app.route('/api/downloads')
def api_downloads():
    with _dl_lock:
        return jsonify(dict(_downloads))


@app.route('/api/cancel/<job_id>', methods=['POST'])
def cancel(job_id):
    with _dl_lock:
        if job_id in _downloads:
            _downloads[job_id]['cancelled'] = True
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=2233, debug=False)

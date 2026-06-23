#!/usr/bin/env python3
"""IPTV Series Downloader — Flask web UI"""

import os
import re
import json
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests as http
from flask import (Flask, render_template, request, redirect,
                   url_for, session, Response, stream_with_context)

VERSION = "1.7.2"

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

@app.context_processor
def inject_version():
    return {'version': VERSION}

CONFIG_DIR  = Path(os.environ.get('CONFIG_DIR',  '/config'))
DOWNLOAD_DIR = Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))
CONFIG_FILE  = CONFIG_DIR / 'accounts.json'

CONNECTION_TIMEOUT = 10
READ_TIMEOUT       = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = re.sub(r'[\s\-]+', '.', name).strip('.') or 'Unknown'
    return name


def episode_filename(show: str, season: int, ep: int, title: str, ext: str) -> str:
    title_part = f'.{sanitize(title)}' if title and title.strip() else ''
    return f'{sanitize(show)}.S{season:02d}E{ep:02d}{title_part}.{ext}'


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


def set_default_account(idx):
    accounts = load_accounts()
    for i, a in enumerate(accounts):
        a['default'] = (i == idx)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)


def save_account(acc):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    accounts = load_accounts()
    # New account becomes the default; clear default on others
    for a in accounts:
        a['default'] = False
    acc['default'] = True
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
# Routes — auth
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    # Auto-connect to the default account if no active session
    if 'server' not in session:
        accounts = load_accounts()
        # Use the account marked default, otherwise fall back to the first saved account
        default = next((a for a in accounts if a.get('default')), None) or (accounts[0] if accounts else None)
        if default:
            client = XtreamClient(default['server'], default['username'], default['password'])
            if client.authenticate():
                session.update(server=default['server'], username=default['username'],
                               password=default['password'], account_name=default['name'])
                return redirect(url_for('browse'))

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
            set_default_account(idx)
        return redirect(url_for('browse'))

    if action == 'delete':
        delete_account(int(request.form.get('account_idx', 0)))
        return redirect(url_for('index'))

    # action == 'login'
    raw    = request.form.get('m3u_url', '').strip()
    manual = request.form.get('manual') == '1'

    if manual or not raw.startswith('http') or 'get.php' not in raw:
        server   = raw
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not server or not username:
            return render_template('login.html', accounts=load_accounts(),
                                   show_manual=True,
                                   error='Server URL en gebruikersnaam zijn verplicht.')
    else:
        parsed = parse_m3u_url(raw)
        if not parsed:
            return render_template('login.html', accounts=load_accounts(),
                                   show_manual=False,
                                   error='Ongeldige M3U+ URL. Verwacht: http://host/get.php?username=X&password=Y')
        server, username, password = parsed

    client = XtreamClient(server, username, password)
    if not client.authenticate():
        return render_template('login.html', accounts=load_accounts(),
                               show_manual=manual or 'get.php' not in raw,
                               error='Authenticatie mislukt — controleer je gegevens.')

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
# Cache
# ---------------------------------------------------------------------------

def _cache_key() -> str:
    raw = f"{session.get('server','')}{session.get('username','')}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _cache_file() -> Path:
    return CONFIG_DIR / f"cache_{_cache_key()}.json"


def load_cache() -> dict:
    f = _cache_file()
    if f.exists():
        with open(f) as fh:
            return json.load(fh)
    return {}


def save_cache(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data['fetched_at'] = datetime.utcnow().isoformat()
    with open(_cache_file(), 'w') as fh:
        json.dump(data, fh)


def clear_cache():
    f = _cache_file()
    if f.exists():
        f.unlink()


def cache_age(cache: dict) -> str:
    ts = cache.get('fetched_at')
    if not ts:
        return ''
    try:
        delta = datetime.utcnow() - datetime.fromisoformat(ts)
        h, m = divmod(int(delta.total_seconds()) // 60, 60)
        if h:
            return f'{h}u {m}m geleden'
        return f'{m}m geleden'
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Routes — browse
# ---------------------------------------------------------------------------

@app.route('/browse')
def browse():
    client = get_client()
    if not client:
        return redirect(url_for('index'))

    cache = load_cache()
    if 'categories' in cache:
        cats  = cache['categories']
        error = None
    else:
        try:
            cats  = client.categories()
            cache['categories'] = cats
            save_cache(cache)
            error = None
        except Exception as e:
            cats, error = [], str(e)

    return render_template('browse.html', categories=cats,
                           cache_age=cache_age(load_cache()), error=error)


@app.route('/category/<cat_id>')
def category(cat_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))

    cache    = load_cache()
    cat_name = request.args.get('name', 'Categorie')
    cached   = cache.get('series_by_cat', {}).get(cat_id)

    if cached is not None:
        return render_template('series_list.html', series_list=cached,
                               title=cat_name, error=None)
    try:
        series_list = client.series(cat_id)
        cache.setdefault('series_by_cat', {})[cat_id] = series_list
        save_cache(cache)
        error = None
    except Exception as e:
        series_list, error = [], str(e)

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

    cache = load_cache()
    if 'series_all' not in cache:
        try:
            cache['series_all'] = client.series()
            save_cache(cache)
        except Exception as e:
            return render_template('series_list.html', series_list=[],
                                   title=f'Zoeken: {q}', error=str(e))

    results = [s for s in cache['series_all'] if q in s.get('name', '').lower()]
    return render_template('series_list.html', series_list=results,
                           title=f'Zoeken: {q}', error=None)


@app.route('/sync', methods=['POST'])
def sync():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    clear_cache()
    # Pre-fetch categories and all series into fresh cache
    try:
        cache = {
            'categories': client.categories(),
            'series_all': client.series(),
            'series_by_cat': {},
        }
        save_cache(cache)
    except Exception:
        pass
    return redirect(url_for('browse'))


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

        # Build list: (season_num, [episode_dicts_with_filename])
        seasons_data = []
        for s in seasons:
            eps = []
            for ep in eps_by_s[s]:
                ep = dict(ep)
                ep['_filename'] = episode_filename(
                    show_name,
                    int(ep.get('season', 1)),
                    int(ep.get('episode_num', 1)),
                    ep.get('title', '') or '',
                    ep.get('container_extension', 'mkv')
                )
                eps.append(ep)
            seasons_data.append((s, eps))

        error = None
    except Exception as e:
        show_name, seasons_data, error = 'Fout', [], str(e)

    return render_template('seasons.html',
                           series_name=show_name,
                           seasons_data=seasons_data,
                           error=error)


# ---------------------------------------------------------------------------
# Route — stream episode to browser
# ---------------------------------------------------------------------------

@app.route('/stream/<ep_id>')
def stream_episode(ep_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))

    ext      = request.args.get('ext', 'mkv')
    filename = request.args.get('filename', f'episode.{ext}')
    url      = client.stream_url(ep_id, ext)

    try:
        upstream = http.get(url, stream=True,
                            timeout=(CONNECTION_TIMEOUT, None),
                            headers={'User-Agent': 'IPTV-Downloader/1.0'})
        upstream.raise_for_status()
    except Exception as e:
        return f'Stream fout: {e}', 502

    def generate():
        for chunk in upstream.iter_content(chunk_size=1024 * 64):
            if chunk:
                yield chunk

    headers = {
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
        'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream'),
    }
    if 'Content-Length' in upstream.headers:
        headers['Content-Length'] = upstream.headers['Content-Length']

    return Response(stream_with_context(generate()), headers=headers)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=2233, debug=False)

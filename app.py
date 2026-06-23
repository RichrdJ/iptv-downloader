#!/usr/bin/env python3
"""IPTV Series Downloader — Flask web UI  v2"""

import os
import re
import json
import hashlib
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests as http
from flask import (Flask, render_template, request, redirect,
                   url_for, session, Response, stream_with_context, jsonify)

VERSION = "2.0.1"

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

@app.context_processor
def inject_globals():
    fav_ids = set()
    fav_movie_ids = set()
    if 'server' in session:
        for f in load_favorites():
            if f.get('type') == 'movie':
                fav_movie_ids.add(f['movie_id'])
            else:
                fav_ids.add(f.get('series_id'))
    return {'version': VERSION, 'fav_ids': fav_ids, 'fav_movie_ids': fav_movie_ids}

CONFIG_DIR   = Path(os.environ.get('CONFIG_DIR',   '/config'))
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


def movie_filename(title: str, year: str, ext: str) -> str:
    year_part = f'.{year}' if year else ''
    return f'{sanitize(title)}{year_part}.{ext}'


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

    # Series
    def categories(self):
        return self._get('get_series_categories') or []

    def series(self, cat_id=None):
        extra = {'category_id': cat_id} if cat_id else None
        return self._get('get_series', extra) or []

    def series_info(self, series_id):
        return self._get('get_series_info', {'series_id': series_id}) or {}

    def stream_url(self, ep_id, ext):
        return f'{self.server}/series/{self.username}/{self.password}/{ep_id}.{ext}'

    # Movies (VOD)
    def movie_categories(self):
        return self._get('get_vod_categories') or []

    def movies(self, cat_id=None):
        extra = {'category_id': cat_id} if cat_id else None
        return self._get('get_vod_streams', extra) or []

    def movie_url(self, movie_id, ext):
        return f'{self.server}/movie/{self.username}/{self.password}/{movie_id}.{ext}'


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
    for a in accounts:
        a['default'] = False
    acc['default'] = True
    accounts.append(acc)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)


def update_account(idx, data):
    accounts = load_accounts()
    if 0 <= idx < len(accounts):
        accounts[idx].update(data)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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
# Cache
# ---------------------------------------------------------------------------

def _cache_key_for(server: str, username: str) -> str:
    return hashlib.md5(f"{server}{username}".encode()).hexdigest()[:12]


def _cache_key() -> str:
    return _cache_key_for(session.get('server', ''), session.get('username', ''))


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
        return f'{h}u {m}m geleden' if h else f'{m}m geleden'
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

def _favs_file() -> Path:
    return CONFIG_DIR / f"favorites_{_cache_key()}.json"


def load_favorites() -> list:
    f = _favs_file()
    return json.load(open(f)) if f.exists() else []


def toggle_favorite(item_id: int, name: str, cover: str, fav_type: str = 'series') -> bool:
    favs = load_favorites()
    id_key = 'movie_id' if fav_type == 'movie' else 'series_id'
    if any(f.get(id_key) == item_id for f in favs):
        favs = [f for f in favs if f.get(id_key) != item_id]
        is_fav = False
    else:
        favs.append({id_key: item_id, 'name': name, 'cover': cover or '', 'type': fav_type})
        is_fav = True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_favs_file(), 'w') as fh:
        json.dump(favs, fh, indent=2)
    return is_fav


# ---------------------------------------------------------------------------
# Download history
# ---------------------------------------------------------------------------

def _hist_file() -> Path:
    return CONFIG_DIR / f"history_{_cache_key()}.json"


def load_history() -> set:
    f = _hist_file()
    return set(json.load(open(f))) if f.exists() else set()


def mark_downloaded(ep_ids: list):
    hist = load_history()
    hist.update(str(i) for i in ep_ids)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_hist_file(), 'w') as fh:
        json.dump(list(hist), fh)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings_file() -> Path:
    return CONFIG_DIR / 'settings.json'


def load_settings() -> dict:
    f = _settings_file()
    return json.load(open(f)) if f.exists() else {'sync_interval': 0}


def save_settings(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_settings_file(), 'w') as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Background auto-sync
# ---------------------------------------------------------------------------

def _auto_sync_worker():
    """Daemon thread: syncs the default account's cache on the configured interval."""
    while True:
        time.sleep(60)
        try:
            interval_h = load_settings().get('sync_interval', 0)
            if not interval_h:
                continue
            accounts = load_accounts()
            acc = next((a for a in accounts if a.get('default')), None) \
                  or (accounts[0] if accounts else None)
            if not acc:
                continue
            key      = _cache_key_for(acc['server'], acc['username'])
            cache_f  = CONFIG_DIR / f"cache_{key}.json"
            if cache_f.exists():
                with open(cache_f) as fh:
                    cache = json.load(fh)
                ts = cache.get('fetched_at')
                if ts:
                    delta = datetime.utcnow() - datetime.fromisoformat(ts)
                    if delta.total_seconds() < interval_h * 3600:
                        continue
            client = XtreamClient(acc['server'], acc['username'], acc['password'])
            new_cache = {
                'categories':    client.categories(),
                'series_all':    client.series(),
                'series_by_cat': {},
                'movie_cats':    client.movie_categories(),
                'movies_all':    client.movies(),
                'movies_by_cat': {},
                'fetched_at':    datetime.utcnow().isoformat(),
            }
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_f, 'w') as fh:
                json.dump(new_cache, fh)
        except Exception:
            pass


_sync_thread = threading.Thread(target=_auto_sync_worker, daemon=True, name='auto-sync')
_sync_thread.start()


# ---------------------------------------------------------------------------
# Routes — auth
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if 'server' not in session:
        accounts = load_accounts()
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

    if action == 'edit':
        idx    = int(request.form.get('account_idx', 0))
        name   = request.form.get('account_name', '').strip()
        server = request.form.get('server', '').strip()
        user   = request.form.get('username', '').strip()
        pwd    = request.form.get('password', '').strip()
        if name or server or user:
            data = {}
            if name:   data['name']     = name
            if server: data['server']   = server
            if user:   data['username'] = user
            if pwd:    data['password'] = pwd
            update_account(idx, data)
        return redirect(url_for('index'))

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
                                   error='Ongeldige M3U+ URL.')
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
# Routes — series browse
# ---------------------------------------------------------------------------

@app.route('/browse')
def browse():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    cache = load_cache()
    if 'categories' in cache:
        cats, error = cache['categories'], None
    else:
        try:
            cats = client.categories()
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
        return render_template('series_list.html', series_list=cached, title=cat_name, error=None)
    try:
        series_list = client.series(cat_id)
        cache.setdefault('series_by_cat', {})[cat_id] = series_list
        save_cache(cache)
        error = None
    except Exception as e:
        series_list, error = [], str(e)
    return render_template('series_list.html', series_list=series_list, title=cat_name, error=error)


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
            return render_template('series_list.html', series_list=[], title=f'Zoeken: {q}', error=str(e))
    results = [s for s in cache['series_all'] if q in s.get('name', '').lower()]
    return render_template('series_list.html', series_list=results, title=f'Zoeken: {q}', error=None)


@app.route('/sync', methods=['POST'])
def sync():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    clear_cache()
    try:
        save_cache({
            'categories':    client.categories(),
            'series_all':    client.series(),
            'series_by_cat': {},
            'movie_cats':    client.movie_categories(),
            'movies_all':    client.movies(),
            'movies_by_cat': {},
        })
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
        meta      = info.get('info', {})
        show_name = meta.get('name', 'Unknown')
        eps_by_s  = info.get('episodes', {})
        seasons   = sorted(eps_by_s.keys(), key=lambda x: int(x))
        history   = load_history()

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
                ep['_downloaded'] = str(ep.get('id')) in history
                eps.append(ep)
            seasons_data.append((s, eps))
        error = None
    except Exception as e:
        meta, show_name, seasons_data, error = {}, 'Fout', [], str(e)

    is_fav = any(f['series_id'] == series_id for f in load_favorites())

    return render_template('seasons.html',
                           series_name=show_name,
                           series_id=series_id,
                           series_meta=meta,
                           seasons_data=seasons_data,
                           is_fav=is_fav,
                           error=error)


# ---------------------------------------------------------------------------
# Routes — movies (VOD)
# ---------------------------------------------------------------------------

@app.route('/movies')
def movies():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    cache = load_cache()
    if 'movie_cats' in cache:
        cats, error = cache['movie_cats'], None
    else:
        try:
            cats = client.movie_categories()
            cache['movie_cats'] = cats
            save_cache(cache)
            error = None
        except Exception as e:
            cats, error = [], str(e)
    return render_template('movies.html', categories=cats, error=error)


@app.route('/movies/category/<cat_id>')
def movies_category(cat_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    cache    = load_cache()
    cat_name = request.args.get('name', 'Films')
    cached   = cache.get('movies_by_cat', {}).get(cat_id)
    if cached is not None:
        return render_template('movie_list.html', movies=cached, title=cat_name, error=None)
    try:
        movie_list = client.movies(cat_id)
        cache.setdefault('movies_by_cat', {})[cat_id] = movie_list
        save_cache(cache)
        error = None
    except Exception as e:
        movie_list, error = [], str(e)
    return render_template('movie_list.html', movies=movie_list, title=cat_name, error=error)


@app.route('/movies/search')
def movies_search():
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    q = request.args.get('q', '').strip().lower()
    if not q:
        return redirect(url_for('movies'))
    cache = load_cache()
    if 'movies_all' not in cache:
        try:
            cache['movies_all'] = client.movies()
            save_cache(cache)
        except Exception as e:
            return render_template('movie_list.html', movies=[], title=f'Zoeken: {q}', error=str(e))
    results = [m for m in cache['movies_all'] if q in m.get('name', '').lower()]
    return render_template('movie_list.html', movies=results, title=f'Zoeken: {q}', error=None)


# ---------------------------------------------------------------------------
# Routes — favorites
# ---------------------------------------------------------------------------

@app.route('/favorites')
def favorites():
    if 'server' not in session:
        return redirect(url_for('index'))
    all_favs     = load_favorites()
    fav_series   = [f for f in all_favs if f.get('type', 'series') == 'series']
    fav_movies   = [f for f in all_favs if f.get('type') == 'movie']
    return render_template('favorites.html', fav_series=fav_series,
                           fav_movies=fav_movies, total=len(all_favs))


@app.route('/favorites/toggle', methods=['POST'])
def fav_toggle():
    if 'server' not in session:
        return jsonify(error='not logged in'), 401
    data     = request.get_json()
    fav_type = data.get('type', 'series')
    item_id  = int(data.get('movie_id' if fav_type == 'movie' else 'series_id', 0))
    name     = data.get('name', '')
    cover    = data.get('cover', '')
    is_fav   = toggle_favorite(item_id, name, cover, fav_type)
    return jsonify(is_fav=is_fav)


# ---------------------------------------------------------------------------
# Routes — download history
# ---------------------------------------------------------------------------

@app.route('/history/add', methods=['POST'])
def history_add():
    if 'server' not in session:
        return jsonify(error='not logged in'), 401
    ep_ids = request.get_json().get('ep_ids', [])
    mark_downloaded(ep_ids)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — stream to browser
# ---------------------------------------------------------------------------

def _proxy_stream(url: str, filename: str):
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


@app.route('/stream/<ep_id>')
def stream_episode(ep_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    ext      = request.args.get('ext', 'mkv')
    filename = request.args.get('filename', f'episode.{ext}')
    return _proxy_stream(client.stream_url(ep_id, ext), filename)


@app.route('/stream/movie/<movie_id>')
def stream_movie(movie_id):
    client = get_client()
    if not client:
        return redirect(url_for('index'))
    ext      = request.args.get('ext', 'mp4')
    filename = request.args.get('filename', f'movie.{ext}')
    return _proxy_stream(client.movie_url(movie_id, ext), filename)


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------

SYNC_OPTIONS = [
    (0,   'Uitgeschakeld'),
    (1,   '1 uur'),
    (6,   '6 uur'),
    (12,  '12 uur'),
    (72,  '3 dagen'),
]


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'server' not in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        interval = int(request.form.get('sync_interval', 0))
        save_settings({'sync_interval': interval})
        return redirect(url_for('settings'))

    s     = load_settings()
    cache = load_cache()
    ts    = cache.get('fetched_at')
    next_sync = None
    if ts and s.get('sync_interval'):
        try:
            last_dt   = datetime.fromisoformat(ts)
            next_dt   = last_dt + timedelta(hours=s['sync_interval'])
            delta_sec = (next_dt - datetime.utcnow()).total_seconds()
            if delta_sec > 0:
                h, rem = divmod(int(delta_sec), 3600)
                m      = rem // 60
                next_sync = f'{h}u {m}m' if h else f'{m}m'
            else:
                next_sync = 'Binnenkort'
        except Exception:
            pass

    return render_template('settings.html',
                           settings=s,
                           sync_options=SYNC_OPTIONS,
                           cache_age=cache_age(cache),
                           next_sync=next_sync)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=2233, debug=False)

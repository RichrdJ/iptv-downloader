#!/usr/bin/env python3
"""IPTV Downloader — Flask web UI."""
import hmac
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)

from iptv import VERSION
from iptv import storage as db
from iptv.downloads import DownloadManager
from iptv.xtream import USER_AGENT, XtreamClient, XtreamError, normalize_server, parse_m3u_url

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('iptv')

APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
VERIFY_SSL = os.environ.get('VERIFY_SSL', 'false').lower() in ('1', 'true', 'yes')

app = Flask(__name__)
app.secret_key = db.secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Accounts die niet zijn opgeslagen leven alleen in het geheugen (nooit in de cookie).
_temp_accounts: dict = {}


# ---------------------------------------------------------------------------
# Naamgeving
# ---------------------------------------------------------------------------

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    """'Breaking Bad - Pilot' -> 'Breaking.Bad.Pilot'."""
    name = _ILLEGAL.sub('', name or '')
    name = re.sub(r'[()\[\]{}!,;]', '', name)
    name = re.sub(r'[\s\-_]+', '.', name)
    name = re.sub(r'\.{2,}', '.', name).strip('.')
    return name or 'Unknown'


def sanitize_folder(name: str) -> str:
    name = _ILLEGAL.sub('', name or '')
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return name[:150] or 'Unknown'


def clean_filename(fn: str, fallback: str) -> str:
    """Door de gebruiker aangepaste naam: geen paden, geen verborgen bestanden."""
    fn = _ILLEGAL.sub('', (fn or '')).replace('..', '.').strip(' .')
    return fn[:220] or fallback


def safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def movie_year(m: dict) -> str:
    for key in ('year', 'releasedate', 'release_date', 'releaseDate', 'name'):
        y = re.search(r'(19|20)\d{2}', str(m.get(key) or ''))
        if y:
            return y.group(0)
    return ''


def episode_filename(show, season, ep, title, ext):
    title_part = ''
    if title and title.strip():
        t = sanitize(title)
        # Veel providers zetten "Show - S01E01 - Titel" in de titel; niet dubbel opnemen.
        t = re.sub(r'^.*?S\d{1,2}E\d{1,3}', '', t, flags=re.I).strip('.')
        if t and t.lower() != sanitize(show).lower():
            title_part = f'.{t}'
    return f'{sanitize(show)}.S{season:02d}E{ep:02d}{title_part}.{ext}'


_TRAILING_YEAR = re.compile(r'\s*[\(\[]?((?:19|20)\d{2})[\)\]]?\s*$')


def strip_year(name):
    return _TRAILING_YEAR.sub('', name or '').strip() or name


def movie_filename(name, year, ext):
    m = _TRAILING_YEAR.search(name or '')
    year = year or (m.group(1) if m else '')
    base = sanitize(re.sub(r'[()\[\]]', '', strip_year(name)))
    if year and year not in base:
        base += f'.{year}'
    return f'{base}.{ext}'


def format_rating(r):
    try:
        v = float(r)
    except Exception:   # ook jinja Undefined
        return ''
    return f'{v:.1f}'.rstrip('0').rstrip('.') if v > 0 else ''


def format_duration(d):
    """'00:42:10' -> '42 min', '01:05:00' -> '1u 5m'."""
    parts = [safe_int(x) for x in str(d or '').split(':')]
    if len(parts) != 3 or not any(parts):
        return ''
    h, m, _ = parts
    return f'{h}u {m}m' if h else f'{m} min'


app.jinja_env.globals.update(movie_filename=movie_filename, movie_year=movie_year)
app.jinja_env.filters['rating'] = format_rating
app.jinja_env.filters['dur'] = format_duration


# ---------------------------------------------------------------------------
# Account / sessie
# ---------------------------------------------------------------------------

def find_account(aid):
    return (db.get_account(aid) or _temp_accounts.get(aid)) if aid else None


def current_account():
    return find_account(session.get('account_id'))


def client_for(acc) -> XtreamClient:
    return XtreamClient(acc['server'], acc['username'], acc['password'], VERIFY_SSL)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        acc = current_account()
        if not acc:
            if request.path.startswith('/api/') or request.is_json:
                return jsonify(error='niet verbonden'), 401
            return redirect(url_for('index'))
        return view(acc, *args, **kwargs)
    return wrapper


@app.before_request
def require_app_password():
    if not APP_PASSWORD or request.endpoint in ('auth', 'static', 'healthz'):
        return None
    if session.get('authed'):
        return None
    if request.path.startswith('/api/'):
        return jsonify(error='niet ingelogd'), 401
    return redirect(url_for('auth', next=request.full_path))


@app.route('/auth', methods=['GET', 'POST'])
def auth():
    error = None
    if request.method == 'POST':
        if hmac.compare_digest(request.form.get('password', '').encode(), APP_PASSWORD.encode()):
            session.permanent = True
            session['authed'] = True
            nxt = request.args.get('next', '/')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/')
        time.sleep(1)  # vertraag brute-force pogingen
        error = 'Onjuist wachtwoord.'
    return render_template('auth.html', error=error)


@app.context_processor
def inject_globals():
    ctx = {'version': VERSION, 'fav_ids': set(), 'fav_movie_ids': set(),
           'account': None, 'app_password': bool(APP_PASSWORD),
           'dl_mode': db.load_settings()['download_mode']}
    acc = current_account()
    if acc:
        ctx['account'] = acc
        for f in db.load_favorites(db.account_key(acc)):
            if f.get('type') == 'movie':
                ctx['fav_movie_ids'].add(f.get('movie_id'))
            else:
                ctx['fav_ids'].add(f.get('series_id'))
    return ctx


# ---------------------------------------------------------------------------
# Catalogus
# ---------------------------------------------------------------------------

_sync_locks: dict = {}


def sync_catalog(acc) -> dict:
    """Haal alles in één keer op. Categoriepagina's filteren daarna lokaal (veel minder API-calls)."""
    key = db.account_key(acc)
    lock = _sync_locks.setdefault(key, threading.Lock())
    with lock:
        c = client_for(acc)
        data = {
            'series_cats': c.series_categories(),
            'series': c.series(),
            'movie_cats': c.movie_categories(),
            'movies': c.movies(),
        }
        try:
            data['account_info'] = c.account_info()
        except XtreamError:
            data['account_info'] = {}
        db.put_cache(key, data)
        log.info('Sync %s: %d series, %d films', acc.get('name'), len(data['series']), len(data['movies']))
        return data


def catalog(acc) -> dict:
    cache = db.get_cache(db.account_key(acc))
    if 'series' not in cache or 'movies' not in cache:   # geen cache, of nog een v2-cache
        cache = sync_catalog(acc)
    return cache


def in_category(item, cat_id):
    if str(item.get('category_id')) == str(cat_id):
        return True
    return any(str(i) == str(cat_id) for i in (item.get('category_ids') or []))


def category_counts(items):
    counts = Counter()
    for it in items:
        ids = {str(it.get('category_id'))} | {str(i) for i in (it.get('category_ids') or [])}
        counts.update(ids)
    return counts


def recent(items, field, n=14):
    return sorted(items, key=lambda i: safe_int(i.get(field)), reverse=True)[:n]


def human_age(seconds):
    if seconds is None:
        return ''
    m = int(seconds // 60)
    if m < 1:
        return 'zojuist'
    if m < 60:
        return f'{m} min geleden'
    h = m // 60
    return f'{h} uur geleden' if h < 48 else f'{h // 24} dagen geleden'


def human_duration(seconds):
    m = int(seconds // 60)
    if m < 60:
        return f'{max(m, 1)} min'
    return f'{m // 60} uur {m % 60} min' if m % 60 else f'{m // 60} uur'


def _auto_sync_worker():
    time.sleep(20)
    while True:
        try:
            interval = db.load_settings()['sync_interval']
            acc = db.default_account()
            if interval and acc:
                age = db.cache_age_seconds(db.get_cache(db.account_key(acc)))
                if age is None or age >= interval * 3600:
                    sync_catalog(acc)
        except Exception as e:
            log.warning('Auto-sync mislukt: %s', e)
        time.sleep(60)


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def download_root() -> Path:
    sub = db.load_settings().get('download_subdir', '').strip().strip('/')
    root = db.DOWNLOAD_DIR.resolve()
    target = (root / sub).resolve() if sub else root
    if target != root and root not in target.parents:
        raise ValueError('Downloadmap moet binnen DOWNLOAD_DIR liggen')
    return target


def build_dest(kind, filename, show='', season=0, title='', year=''):
    root = download_root()
    if db.load_settings()['organize'] == 'folders':
        if kind == 'episode':
            folder = root / 'Series' / sanitize_folder(show) / f'Season {season:02d}'
        else:
            title = strip_year(title)
            folder = root / 'Films' / sanitize_folder(f'{title} ({year})' if year else title)
    else:
        folder = root
    dest = (folder / filename).resolve()
    if root not in dest.parents:
        raise ValueError('Ongeldige bestandsnaam')
    return dest


def _resolve_url(job):
    acc = find_account(job['account_id'])
    if not acc:
        raise IOError('Account bestaat niet meer')
    c = client_for(acc)
    url = c.movie_url(job['item_id'], job['ext']) if job['kind'] == 'movie' \
        else c.episode_url(job['item_id'], job['ext'])
    return url, VERIFY_SSL


def _on_complete(job):
    db.mark_downloaded(job['account_key'], [job['history_id']])


downloads = DownloadManager(
    resolve_url=_resolve_url,
    get_limit=lambda: db.load_settings()['max_concurrent'],
    on_complete=_on_complete,
)
threading.Thread(target=_auto_sync_worker, daemon=True, name='auto-sync').start()


# ---------------------------------------------------------------------------
# Routes — verbinden
# ---------------------------------------------------------------------------

def login_page(error=None, manual=None):
    manual = request.args.get('manual') == '1' if manual is None else manual
    return render_template('login.html', accounts=db.load_accounts(),
                           show_manual=manual, error=error)


def urlhost(server):
    return re.sub(r'^https?://', '', server)


@app.route('/')
def index():
    if current_account():
        return redirect(url_for('browse'))
    acc = db.default_account()
    if acc and request.args.get('switch') != '1':
        # Niet elke keer opnieuw authenticeren: een verse cache is bewijs genoeg.
        age = db.cache_age_seconds(db.get_cache(db.account_key(acc)))
        if (age is not None and age < 86400) or client_for(acc).authenticate():
            session.permanent = True
            session['account_id'] = acc['id']
            return redirect(url_for('browse'))
    return login_page()


@app.route('/connect', methods=['POST'])
def connect():
    action = request.form.get('action', 'login')
    aid = request.form.get('account_id', '')

    if action == 'select':
        acc = db.get_account(aid)
        if acc:
            if not client_for(acc).authenticate():
                return login_page(f'Kan niet verbinden met “{acc["name"]}”. Controleer de gegevens.')
            session.permanent = True
            session['account_id'] = acc['id']
            db.set_default_account(acc['id'])
        return redirect(url_for('browse'))

    if action == 'delete':
        db.delete_account(aid)
        return redirect(url_for('index', switch=1))

    if action == 'edit':
        db.update_account(aid, {
            'name': request.form.get('account_name', '').strip(),
            'server': normalize_server(request.form.get('server', '')),
            'username': request.form.get('username', '').strip(),
            'password': request.form.get('password', '').strip(),
        })
        return redirect(url_for('index', switch=1))

    raw = request.form.get('m3u_url', '').strip()
    parsed = parse_m3u_url(raw) if 'get.php' in raw else None
    if parsed:
        server, username, password = parsed
    else:
        server = normalize_server(raw)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not server or not username:
            return login_page('Vul een geldige M3U+ URL in, of server + gebruikersnaam.', manual=True)

    acc = {'name': request.form.get('account_name', '').strip() or f'{username}@{urlhost(server)}',
           'server': server, 'username': username, 'password': password}
    if not client_for(acc).authenticate():
        return login_page('Authenticatie mislukt — controleer je gegevens.', manual=not parsed)

    if request.form.get('save_account'):
        acc = db.add_account(acc)
    else:
        acc['id'] = 'tmp-' + uuid.uuid4().hex[:10]
        _temp_accounts[acc['id']] = acc
    session.permanent = True
    session['account_id'] = acc['id']
    return redirect(url_for('browse'))


@app.route('/logout')
def logout():
    session.pop('account_id', None)
    return redirect(url_for('index', switch=1))


# ---------------------------------------------------------------------------
# Routes — series
# ---------------------------------------------------------------------------

def _browse(acc, kind):
    error = None
    try:
        cat = catalog(acc)
    except XtreamError as e:
        cat, error = {}, str(e)
    items = cat.get('series' if kind == 'series' else 'movies', [])
    counts = category_counts(items)
    cats = [dict(c, count=counts.get(str(c.get('category_id')), 0))
            for c in cat.get('series_cats' if kind == 'series' else 'movie_cats', [])]
    return render_template('browse.html', kind=kind, categories=cats,
                           recent=recent(items, 'last_modified' if kind == 'series' else 'added'),
                           total=len(items), cache_age=human_age(db.cache_age_seconds(cat)),
                           history=db.load_history(db.account_key(acc)), error=error)


@app.route('/browse')
@login_required
def browse(acc):
    return _browse(acc, 'series')


@app.route('/category/<cat_id>')
@login_required
def category(acc, cat_id):
    cat = catalog(acc)
    name = next((c.get('category_name') for c in cat.get('series_cats', [])
                 if str(c.get('category_id')) == cat_id), 'Categorie')
    items = [s for s in cat.get('series', []) if in_category(s, cat_id)]
    return render_template('grid.html', kind='series', items=items, title=name,
                           back=url_for('browse'), history=set())


@app.route('/series/<int:series_id>')
@login_required
def series(acc, series_id):
    error, meta, seasons_data, show_name = None, {}, [], 'Onbekend'
    try:
        info = client_for(acc).series_info(series_id)
        meta = info['info']
        if not meta.get('name'):   # sommige providers laten info leeg -> uit catalogus halen
            meta = {**next((s for s in catalog(acc).get('series', [])
                            if safe_int(s.get('series_id')) == series_id), {}), **meta}
        show_name = meta.get('name') or 'Onbekend'
        history = db.load_history(db.account_key(acc))
        for s in sorted(info['episodes'], key=safe_int):
            eps = []
            for ep in info['episodes'][s] or []:
                season = safe_int(ep.get('season'), safe_int(s, 1))
                num = safe_int(ep.get('episode_num'), len(eps) + 1)
                ext = ep.get('container_extension') or 'mkv'
                ep_info = ep.get('info') if isinstance(ep.get('info'), dict) else {}
                eps.append({
                    'id': ep.get('id'), 'num': num, 'season': season, 'ext': ext,
                    'title': ep.get('title') or '',
                    'duration': ep_info.get('duration') or '',
                    'plot': ep_info.get('plot') or '',
                    'filename': episode_filename(show_name, season, num, ep.get('title'), ext),
                    'downloaded': str(ep.get('id')) in history,
                })
            eps.sort(key=lambda e: e['num'])
            seasons_data.append((safe_int(s, 1), eps))
    except XtreamError as e:
        error = str(e)

    backdrop = meta.get('backdrop_path')
    if isinstance(backdrop, list):
        backdrop = backdrop[0] if backdrop else ''
    is_fav = any(f.get('series_id') == series_id for f in db.load_favorites(db.account_key(acc)))
    return render_template('seasons.html', series_name=show_name, series_id=series_id,
                           meta=meta, backdrop=backdrop or '', seasons_data=seasons_data,
                           is_fav=is_fav, error=error)


# ---------------------------------------------------------------------------
# Routes — films
# ---------------------------------------------------------------------------

@app.route('/movies')
@login_required
def movies(acc):
    return _browse(acc, 'movie')


@app.route('/movies/category/<cat_id>')
@login_required
def movies_category(acc, cat_id):
    cat = catalog(acc)
    name = next((c.get('category_name') for c in cat.get('movie_cats', [])
                 if str(c.get('category_id')) == cat_id), 'Films')
    items = [m for m in cat.get('movies', []) if in_category(m, cat_id)]
    return render_template('grid.html', kind='movie', items=items, title=name,
                           back=url_for('movies'), history=db.load_history(db.account_key(acc)))


# ---------------------------------------------------------------------------
# Routes — zoeken, sync, favorieten
# ---------------------------------------------------------------------------

@app.route('/search')
@login_required
def search(acc):
    q = request.args.get('q', '').strip()
    if not q:
        return redirect(url_for('browse'))
    words = q.lower().split()
    cat = catalog(acc)

    def match(item):
        name = (item.get('name') or '').lower()
        return all(w in name for w in words)

    return render_template('search.html', q=q,
                           series=[s for s in cat.get('series', []) if match(s)][:300],
                           movies=[m for m in cat.get('movies', []) if match(m)][:300],
                           history=db.load_history(db.account_key(acc)))


@app.route('/movies/search')
def movies_search():  # v2-compatibiliteit
    return redirect(url_for('search', q=request.args.get('q', '')))


@app.route('/sync', methods=['POST'])
@login_required
def sync(acc):
    try:
        sync_catalog(acc)
    except XtreamError as e:
        log.warning('Handmatige sync mislukt: %s', e)
    ref = request.referrer or ''
    return redirect(ref if ref.startswith(request.host_url) else url_for('browse'))


@app.route('/favorites')
@login_required
def favorites(acc):
    favs = db.load_favorites(db.account_key(acc))
    return render_template('favorites.html',
                           fav_series=[f for f in favs if f.get('type', 'series') == 'series'],
                           fav_movies=[f for f in favs if f.get('type') == 'movie'],
                           total=len(favs))


@app.route('/favorites/toggle', methods=['POST'])
@login_required
def fav_toggle(acc):
    data = request.get_json(silent=True) or {}
    fav_type = 'movie' if data.get('type') == 'movie' else 'series'
    item_id = safe_int(data.get('movie_id' if fav_type == 'movie' else 'series_id') or data.get('id'))
    if not item_id:
        return jsonify(error='ongeldig id'), 400
    is_fav = db.toggle_favorite(db.account_key(acc), fav_type, item_id,
                                str(data.get('name', ''))[:300], str(data.get('cover', ''))[:1000])
    return jsonify(is_fav=is_fav)


@app.route('/history/add', methods=['POST'])
@login_required
def history_add(acc):
    ids = (request.get_json(silent=True) or {}).get('ep_ids', [])
    db.mark_downloaded(db.account_key(acc), [str(i) for i in ids][:2000])
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — browserdownload (proxy)
# ---------------------------------------------------------------------------

def _proxy_stream(url, filename):
    headers = {'User-Agent': USER_AGENT}
    if request.headers.get('Range'):
        headers['Range'] = request.headers['Range']   # laat de browser hervatten
    try:
        upstream = requests.get(url, stream=True, timeout=(15, 60), headers=headers, verify=VERIFY_SSL)
        upstream.raise_for_status()
    except requests.HTTPError as e:
        return f'Provider gaf HTTP {e.response.status_code}', 502
    except requests.RequestException:
        return 'Kan stream niet openen bij de provider', 502

    def generate():
        try:
            yield from upstream.iter_content(chunk_size=256 * 1024)
        finally:
            upstream.close()

    out = {
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
        'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream'),
        'Accept-Ranges': 'bytes',
    }
    for h in ('Content-Length', 'Content-Range'):
        if h in upstream.headers:
            out[h] = upstream.headers[h]
    return Response(stream_with_context(generate()), status=upstream.status_code, headers=out)


def _ext(default):
    return re.sub(r'\W', '', request.args.get('ext', default))[:5] or default


@app.route('/stream/<ep_id>')
@login_required
def stream_episode(acc, ep_id):
    if not ep_id.isdigit():
        return 'Ongeldig id', 400
    ext = _ext('mkv')
    return _proxy_stream(client_for(acc).episode_url(ep_id, ext),
                         clean_filename(request.args.get('filename'), f'episode.{ext}'))


@app.route('/stream/movie/<movie_id>')
@login_required
def stream_movie(acc, movie_id):
    if not movie_id.isdigit():
        return 'Ongeldig id', 400
    ext = _ext('mp4')
    return _proxy_stream(client_for(acc).movie_url(movie_id, ext),
                         clean_filename(request.args.get('filename'), f'movie.{ext}'))


# ---------------------------------------------------------------------------
# Routes — serverdownloads (wachtrij)
# ---------------------------------------------------------------------------

@app.route('/downloads')
@login_required
def downloads_page(acc):
    return render_template('downloads.html', settings=db.load_settings())


@app.route('/api/downloads', methods=['GET'])
def api_downloads():
    root = str(db.DOWNLOAD_DIR.resolve())
    jobs = downloads.list()
    for j in jobs:
        j.pop('account_key', None)
        j['dest'] = j['dest'].replace(root, '', 1).lstrip('/')
    return jsonify(jobs=jobs, summary=downloads.summary())


@app.route('/api/downloads/summary')
def api_downloads_summary():
    return jsonify(downloads.summary())


@app.route('/api/downloads', methods=['POST'])
@login_required
def api_downloads_add(acc):
    """Body: {items: [{type: 'episode'|'movie', id, ext, filename, show?, season?, title?, year?}]}"""
    data = request.get_json(silent=True) or {}
    items = data.get('items') or ([data] if data.get('id') else [])
    added, skipped, errors = 0, 0, []
    key = db.account_key(acc)
    for it in items[:1000]:
        kind = 'movie' if it.get('type') == 'movie' else 'episode'
        item_id = str(it.get('id', '')).strip()
        if not item_id.isdigit():
            errors.append('ongeldig id')
            continue
        ext = re.sub(r'\W', '', str(it.get('ext') or ('mp4' if kind == 'movie' else 'mkv')))[:5]
        filename = clean_filename(it.get('filename'), f'{item_id}.{ext}')
        try:
            dest = build_dest(kind, filename, show=it.get('show', ''), season=safe_int(it.get('season'), 1),
                              title=it.get('title', ''), year=str(it.get('year') or ''))
        except ValueError as e:
            errors.append(str(e))
            continue
        job = downloads.add(account_id=acc['id'], account_key=key, kind=kind, item_id=item_id,
                            ext=ext, dest=dest, label=str(it.get('label') or filename)[:200],
                            history_id=item_id if kind == 'episode' else f'm{item_id}')
        if job:
            added += 1
        else:
            skipped += 1
    status = 200 if added or skipped else 400
    return jsonify(ok=bool(added or skipped), added=added, skipped=skipped, errors=errors[:5]), status


@app.route('/download/server', methods=['POST'])
def download_server_compat():  # v2-endpoint
    return api_downloads_add()


@app.route('/api/downloads/<job_id>/cancel', methods=['POST'])
def api_cancel(job_id):
    return jsonify(ok=downloads.cancel(job_id))


@app.route('/api/downloads/<job_id>/retry', methods=['POST'])
def api_retry(job_id):
    return jsonify(ok=downloads.retry(job_id))


@app.route('/api/downloads/clear', methods=['POST'])
def api_clear():
    downloads.clear_finished()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — instellingen
# ---------------------------------------------------------------------------

SYNC_OPTIONS = [(0, 'Uit'), (1, '1 uur'), (6, '6 uur'), (12, '12 uur'), (24, '1 dag'), (72, '3 dagen')]


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings(acc):
    error = None
    if request.method == 'POST':
        f = request.form
        sub = f.get('download_subdir', '').strip().strip('/')
        if '..' in Path(sub).parts:
            error = 'Submap mag geen “..” bevatten.'
        else:
            db.save_settings({
                'download_mode': 'server' if f.get('download_mode') == 'server' else 'browser',
                'download_subdir': sub,
                'organize': 'folders' if f.get('organize') == 'folders' else 'flat',
                'max_concurrent': max(1, min(10, safe_int(f.get('max_concurrent'), 1))),
                'sync_interval': safe_int(f.get('sync_interval'), 0),
            })
            return redirect(url_for('settings', saved=1))

    s = db.load_settings()
    cache = db.get_cache(db.account_key(acc))
    age = db.cache_age_seconds(cache)
    next_sync = None
    if s['sync_interval'] and age is not None:
        remaining = s['sync_interval'] * 3600 - age
        next_sync = f'over {human_duration(remaining)}' if remaining > 60 else 'binnenkort'

    info = cache.get('account_info') or {}
    exp = safe_int(info.get('exp_date'))
    return render_template('settings.html', settings=s, sync_options=SYNC_OPTIONS,
                           cache_age=human_age(age), next_sync=next_sync, error=error,
                           saved=request.args.get('saved'), download_dir=str(db.DOWNLOAD_DIR),
                           acc_info=info, verify_ssl=VERIFY_SSL,
                           expires=time.strftime('%d-%m-%Y', time.localtime(exp)) if exp else '')


@app.route('/healthz')
def healthz():
    return jsonify(ok=True, version=VERSION)


@app.errorhandler(XtreamError)
def handle_xtream_error(e):
    if request.path.startswith('/api/'):
        return jsonify(error=str(e)), 502
    return render_template('error.html', error=str(e)), 502


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 2233)), debug=False, threaded=True)

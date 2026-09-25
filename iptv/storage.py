"""Persistente opslag in /config: atomische JSON-writes en een in-memory cache."""
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/config'))
DOWNLOAD_DIR = Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))
ACCOUNTS_FILE = CONFIG_DIR / 'accounts.json'
SETTINGS_FILE = CONFIG_DIR / 'settings.json'

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    'sync_interval': 0,          # uren, 0 = uit
    'download_mode': 'browser',  # browser | server
    'download_subdir': '',       # submap binnen DOWNLOAD_DIR
    'organize': 'flat',          # flat | folders (Plex/Jellyfin-structuur)
    'max_concurrent': 1,         # gelijktijdige serverdownloads
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def read_json(path: Path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning('Kan %s niet lezen: %s', path, e)
        return default


def write_json(path: Path, data, indent=2):
    """Atomisch schrijven: nooit een half bestand bij een crash of gelijktijdige write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=indent, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------

def secret_key() -> str:
    """SECRET_KEY uit env, anders één keer genereren en bewaren (sessies overleven herstarts)."""
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    f = CONFIG_DIR / 'secret_key'
    try:
        return f.read_text().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(key)
        f.chmod(0o600)
        return key


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def account_key(acc: dict) -> str:
    """Zelfde sleutel als v2, zodat bestaande cache/favorieten/historie behouden blijven."""
    return hashlib.md5(f"{acc.get('server', '')}{acc.get('username', '')}".encode()).hexdigest()[:12]


def load_accounts() -> list:
    with _lock:
        accounts = read_json(ACCOUNTS_FILE, [])
        changed = False
        for a in accounts:
            if not a.get('id'):          # migratie van v2 (index-gebaseerd)
                a['id'] = uuid.uuid4().hex[:12]
                changed = True
        if changed:
            write_json(ACCOUNTS_FILE, accounts)
        return accounts


def save_accounts(accounts: list):
    write_json(ACCOUNTS_FILE, accounts)
    try:
        ACCOUNTS_FILE.chmod(0o600)
    except OSError:
        pass


def get_account(account_id: str):
    return next((a for a in load_accounts() if a['id'] == account_id), None)


def default_account():
    accs = load_accounts()
    return next((a for a in accs if a.get('default')), None) or (accs[0] if accs else None)


def add_account(acc: dict) -> dict:
    with _lock:
        accounts = load_accounts()
        # Zelfde server + gebruiker? Dan bijwerken i.p.v. dubbel opslaan.
        existing = next((a for a in accounts
                         if a['server'] == acc['server'] and a['username'] == acc['username']), None)
        for a in accounts:
            a['default'] = False
        if existing:
            existing.update(acc, default=True)
            acc = existing
        else:
            acc = {**acc, 'id': uuid.uuid4().hex[:12], 'default': True}
            accounts.append(acc)
        save_accounts(accounts)
        return acc


def update_account(account_id: str, data: dict):
    with _lock:
        accounts = load_accounts()
        for a in accounts:
            if a['id'] == account_id:
                a.update({k: v for k, v in data.items() if v})
        save_accounts(accounts)


def set_default_account(account_id: str):
    with _lock:
        accounts = load_accounts()
        for a in accounts:
            a['default'] = a['id'] == account_id
        save_accounts(accounts)


def delete_account(account_id: str):
    with _lock:
        save_accounts([a for a in load_accounts() if a['id'] != account_id])


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    s = {**DEFAULT_SETTINGS, **read_json(SETTINGS_FILE, {})}
    try:
        s['max_concurrent'] = max(1, min(10, int(s['max_concurrent'])))
        s['sync_interval'] = int(s['sync_interval'])
    except (TypeError, ValueError):
        s['max_concurrent'], s['sync_interval'] = 1, 0
    return s


def save_settings(data: dict):
    current = read_json(SETTINGS_FILE, {})
    current.update(data)
    current.pop('download_path', None)   # v2-instelling, vervangen door download_subdir
    write_json(SETTINGS_FILE, current)


# ---------------------------------------------------------------------------
# Catalogus-cache (in memory + op schijf)
# ---------------------------------------------------------------------------

_cache_mem: dict = {}


def _cache_file(key: str) -> Path:
    return CONFIG_DIR / f'cache_{key}.json'


def get_cache(key: str) -> dict:
    with _lock:
        if key not in _cache_mem:
            _cache_mem[key] = read_json(_cache_file(key), {})
        return _cache_mem[key]


def put_cache(key: str, data: dict):
    data['fetched_at'] = utcnow().isoformat()
    with _lock:
        _cache_mem[key] = data
        write_json(_cache_file(key), data, indent=None)


def cache_age_seconds(cache: dict):
    dt = parse_ts(cache.get('fetched_at'))
    return (utcnow() - dt).total_seconds() if dt else None


# ---------------------------------------------------------------------------
# Favorieten & downloadgeschiedenis (per account)
# ---------------------------------------------------------------------------

def load_favorites(key: str) -> list:
    return read_json(CONFIG_DIR / f'favorites_{key}.json', [])


def toggle_favorite(key: str, fav_type: str, item_id: int, name: str, cover: str) -> bool:
    id_key = 'movie_id' if fav_type == 'movie' else 'series_id'
    with _lock:
        favs = load_favorites(key)
        if any(f.get(id_key) == item_id for f in favs):
            favs = [f for f in favs if f.get(id_key) != item_id]
            is_fav = False
        else:
            favs.append({id_key: item_id, 'name': name, 'cover': cover or '', 'type': fav_type})
            is_fav = True
        write_json(CONFIG_DIR / f'favorites_{key}.json', favs)
        return is_fav


def load_history(key: str) -> set:
    return {str(i) for i in read_json(CONFIG_DIR / f'history_{key}.json', [])}


def mark_downloaded(key: str, ids):
    with _lock:
        hist = load_history(key)
        hist.update(str(i) for i in ids)
        write_json(CONFIG_DIR / f'history_{key}.json', sorted(hist), indent=None)

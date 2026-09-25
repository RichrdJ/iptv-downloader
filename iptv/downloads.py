"""Download-wachtrij voor serverdownloads.

- Configureerbaar aantal gelijktijdige downloads (IPTV-providers staan meestal maar 1-2 verbindingen toe)
- Schrijft naar <bestand>.part en hernoemt pas als het compleet is
- Hervat onderbroken downloads met HTTP Range en probeert automatisch opnieuw
- Wachtrij overleeft een herstart van de container
"""
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import requests

from .storage import CONFIG_DIR, read_json, utcnow, write_json
from .xtream import USER_AGENT

log = logging.getLogger(__name__)

STATE_FILE = CONFIG_DIR / 'downloads.json'
MAX_RETRIES = 4
CHUNK = 1024 * 1024
KEEP_FINISHED = 300
ACTIVE = ('queued', 'downloading')
PERMANENT = (400, 401, 404, 410)   # geen zin om opnieuw te proberen


class Cancelled(Exception):
    pass


class DownloadManager:
    def __init__(self, resolve_url, get_limit, on_complete):
        """
        resolve_url(job) -> (url, verify_ssl)   bouwt de stream-URL op het moment van downloaden
        get_limit()      -> int                 max. aantal gelijktijdige downloads
        on_complete(job)                        callback na een geslaagde download
        """
        self._resolve_url = resolve_url
        self._get_limit = get_limit
        self._on_complete = on_complete
        self._cond = threading.Condition(threading.RLock())
        self._jobs: dict = {}
        self._cancel: set = set()
        self._load()
        threading.Thread(target=self._dispatcher, daemon=True, name='dl-dispatcher').start()

    # -- persistence ---------------------------------------------------------
    def _load(self):
        for job in read_json(STATE_FILE, []):
            if job.get('status') == 'downloading':
                job['status'] = 'queued'        # was bezig tijdens herstart -> hervatten
            job.update(speed=0)
            self._jobs[job['id']] = job

    def _save(self):
        with self._cond:
            jobs = list(self._jobs.values())
            finished = [j for j in jobs if j['status'] not in ACTIVE]
            if len(finished) > KEEP_FINISHED:
                drop = {j['id'] for j in sorted(finished, key=lambda j: j['created'])[:-KEEP_FINISHED]}
                for jid in drop:
                    self._jobs.pop(jid, None)
                jobs = list(self._jobs.values())
            try:
                write_json(STATE_FILE, jobs, indent=None)
            except OSError as e:
                log.error('Kan downloadstatus niet opslaan: %s', e)

    # -- public API ------------------------------------------------------------
    def add(self, *, account_id, account_key, kind, item_id, ext, dest: Path, label, history_id):
        with self._cond:
            for j in self._jobs.values():
                if j['dest'] == str(dest) and j['status'] in ACTIVE:
                    return None                  # staat al in de wachtrij
            job = {
                'id': uuid.uuid4().hex[:10],
                'account_id': account_id, 'account_key': account_key,
                'kind': kind, 'item_id': str(item_id), 'ext': ext,
                'dest': str(dest), 'filename': dest.name, 'label': label,
                'history_id': history_id,
                'status': 'queued', 'bytes': 0, 'total': 0, 'speed': 0,
                'error': '', 'attempt': 0,
                'created': utcnow().isoformat(), 'finished': None,
            }
            self._jobs[job['id']] = job
            self._save()
            self._cond.notify_all()
            return job

    def list(self):
        with self._cond:
            return sorted((dict(j) for j in self._jobs.values()), key=lambda j: j['created'])

    def summary(self):
        with self._cond:
            jobs = self._jobs.values()
            return {
                'active': sum(1 for j in jobs if j['status'] == 'downloading'),
                'queued': sum(1 for j in jobs if j['status'] == 'queued'),
                'failed': sum(1 for j in jobs if j['status'] == 'error'),
            }

    def cancel(self, job_id):
        with self._cond:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job['status'] == 'queued':
                job['status'] = 'cancelled'
                self._save()
            elif job['status'] == 'downloading':
                self._cancel.add(job_id)
            return True

    def retry(self, job_id):
        with self._cond:
            job = self._jobs.get(job_id)
            if job and job['status'] in ('error', 'cancelled'):
                job.update(status='queued', error='', attempt=0, speed=0)
                self._save()
                self._cond.notify_all()
                return True
            return False

    def clear_finished(self):
        with self._cond:
            for jid in [j['id'] for j in self._jobs.values() if j['status'] not in ACTIVE]:
                del self._jobs[jid]
            self._save()

    # -- worker ------------------------------------------------------------------
    def _dispatcher(self):
        while True:
            with self._cond:
                self._cond.wait(timeout=3)
                running = sum(1 for j in self._jobs.values() if j['status'] == 'downloading')
                free = max(1, self._get_limit()) - running
                started = False
                for job in sorted(self._jobs.values(), key=lambda j: j['created']):
                    if free <= 0:
                        break
                    if job['status'] == 'queued':
                        job.update(status='downloading', error='')
                        threading.Thread(target=self._run, args=(job,), daemon=True,
                                         name=f"dl-{job['id']}").start()
                        free -= 1
                        started = True
                if started:
                    self._save()

    def _backoff(self, job, attempt):
        """Oplopend wachten (10s, 20s, 30s), maar annuleerbaar."""
        if attempt >= MAX_RETRIES:
            return
        for _ in range(10 * attempt):
            self._check_cancel(job)
            time.sleep(1)

    def _check_cancel(self, job):
        if job['id'] in self._cancel:
            raise Cancelled()

    def _run(self, job):
        dest = Path(job['dest'])
        part = dest.with_name(dest.name + '.part')
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size > 0:
                size = dest.stat().st_size
                return self._finish(job, 'done', bytes=size, total=size, error='Bestond al')

            last_error = ''
            for attempt in range(1, MAX_RETRIES + 1):
                job['attempt'] = attempt
                try:
                    self._transfer(job, part)
                    os.replace(part, dest)
                    self._finish(job, 'done')
                    try:
                        self._on_complete(job)
                    except Exception:
                        log.exception('on_complete mislukt')
                    return
                except Cancelled:
                    raise
                except requests.HTTPError as e:
                    last_error = _friendly(e)
                    if e.response is not None and e.response.status_code in PERMANENT:
                        break                      # opnieuw proberen heeft geen zin
                    job.update(error=f'{last_error} — poging {attempt}/{MAX_RETRIES}', speed=0)
                    self._backoff(job, attempt)
                except Exception as e:
                    last_error = _friendly(e)
                    job.update(error=f'{last_error} — poging {attempt}/{MAX_RETRIES}', speed=0)
                    log.warning('Download %s mislukt (poging %d): %s', job['filename'], attempt, last_error)
                    self._backoff(job, attempt)
            self._finish(job, 'error', error=last_error)
        except Cancelled:
            part.unlink(missing_ok=True)
            self._finish(job, 'cancelled', error='')
        except Exception as e:
            log.exception('Onverwachte fout bij %s', job['filename'])
            self._finish(job, 'error', error=_friendly(e))

    def _transfer(self, job, part: Path):
        url, verify = self._resolve_url(job)
        start = part.stat().st_size if part.exists() else 0
        headers = {'User-Agent': USER_AGENT}
        if start:
            headers['Range'] = f'bytes={start}-'

        with requests.get(url, stream=True, headers=headers, timeout=(15, 60), verify=verify) as r:
            if r.status_code == 416:            # .part is al compleet of ongeldig -> opnieuw beginnen
                part.unlink(missing_ok=True)
                raise IOError('Hervatten niet mogelijk, begin opnieuw')
            r.raise_for_status()
            if start and r.status_code != 206:  # server negeert Range
                start = 0
            total = _total_size(r, start)
            job.update(bytes=start, total=total)

            window_t, window_b = time.monotonic(), start
            with open(part, 'ab' if start else 'wb') as fh:
                for chunk in r.iter_content(CHUNK):
                    self._check_cancel(job)
                    if not chunk:
                        continue
                    fh.write(chunk)
                    job['bytes'] += len(chunk)
                    now = time.monotonic()
                    if now - window_t >= 1:
                        job['speed'] = int((job['bytes'] - window_b) / (now - window_t))
                        window_t, window_b = now, job['bytes']

        if total and job['bytes'] < total:
            raise IOError(f"Onvolledig: {job['bytes']} van {total} bytes")
        if job['bytes'] == 0:
            raise IOError('Leeg bestand ontvangen')

    def _finish(self, job, status, **extra):
        with self._cond:
            self._cancel.discard(job['id'])
            job.update(status=status, speed=0, finished=utcnow().isoformat(), **extra)
            if status == 'done' and not job.get('total'):
                job['total'] = job['bytes']
            self._save()
            self._cond.notify_all()


def _total_size(r, start):
    cr = r.headers.get('Content-Range', '')
    m = re.search(r'/(\d+)$', cr)
    if m:
        return int(m.group(1))
    try:
        return int(r.headers.get('Content-Length', 0)) + start
    except ValueError:
        return 0


def _friendly(e: Exception) -> str:
    """Foutmelding zonder URL (daar staat het wachtwoord in)."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        hint = {401: 'geweigerd', 403: 'geweigerd (max. verbindingen bereikt?)',
                404: 'niet gevonden', 429: 'te veel verzoeken', 458: 'max. verbindingen bereikt'}
        return f'HTTP {code} {hint.get(code, "")}'.strip()
    if isinstance(e, requests.Timeout):
        return 'Timeout'
    if isinstance(e, requests.ConnectionError):
        return 'Verbinding verbroken'
    if isinstance(e, OSError) and e.errno == 28:
        return 'Schijf vol'
    if isinstance(e, PermissionError):
        return 'Geen schrijfrechten op downloadmap'
    msg = str(e)
    return re.sub(r'https?://\S+', '[url]', msg)[:200] or e.__class__.__name__

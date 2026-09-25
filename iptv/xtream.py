"""Xtream Codes API client."""
import logging
import re
from urllib.parse import parse_qs, urlparse

import requests
import urllib3

log = logging.getLogger(__name__)

USER_AGENT = 'IPTV-Downloader/3.0'
API_TIMEOUT = (10, 45)

# Veel IPTV-providers hebben geen geldig certificaat; waarschuwingen niet in de log spammen.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class XtreamError(Exception):
    """Fout richting de provider. De tekst is veilig om te tonen (bevat nooit credentials)."""


def normalize_server(server: str) -> str:
    """'example.com:8080/get.php' -> 'http://example.com:8080'."""
    server = (server or '').strip()
    if not server:
        return ''
    if not re.match(r'^https?://', server, re.I):
        server = 'http://' + server
    p = urlparse(server)
    return f'{p.scheme.lower()}://{p.netloc}'


def parse_m3u_url(url: str):
    """Haal (server, username, password) uit een M3U+ get.php URL. Parametervolgorde maakt niet uit."""
    try:
        p = urlparse((url or '').strip())
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or not p.netloc:
        return None
    q = parse_qs(p.query)
    user = (q.get('username') or [None])[0]
    pwd = (q.get('password') or [None])[0]
    if not user or not pwd:
        return None
    return f'{p.scheme}://{p.netloc}', user, pwd


def _as_list(data):
    """Providers geven soms een dict of {} terug waar een lijst verwacht wordt."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [v for v in data.values() if isinstance(v, dict)]
    return []


class XtreamClient:
    def __init__(self, server: str, username: str, password: str, verify_ssl: bool = False):
        self.server = normalize_server(server)
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.session.verify = verify_ssl

    # -- low level ---------------------------------------------------------
    def _get(self, action=None, **params):
        query = {'username': self.username, 'password': self.password, **params}
        if action:
            query['action'] = action
        try:
            r = self.session.get(f'{self.server}/player_api.php', params=query, timeout=API_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # Nooit str(e) tonen: daar staat de URL mét wachtwoord in.
            raise XtreamError(f'Provider gaf HTTP {e.response.status_code}') from None
        except requests.Timeout:
            raise XtreamError('Provider reageert niet (timeout)') from None
        except requests.ConnectionError:
            raise XtreamError('Kan geen verbinding maken met de provider') from None
        except ValueError:
            raise XtreamError('Ongeldig antwoord van de provider') from None

    # -- account -------------------------------------------------------------
    def account_info(self) -> dict:
        """Geeft user_info terug, of {} als de login ongeldig is."""
        data = self._get()
        info = data.get('user_info', {}) if isinstance(data, dict) else {}
        return info if str(info.get('auth', 0)) == '1' else {}

    def authenticate(self) -> bool:
        try:
            return bool(self.account_info())
        except XtreamError:
            return False

    # -- series --------------------------------------------------------------
    def series_categories(self):
        return _as_list(self._get('get_series_categories'))

    def series(self):
        return _as_list(self._get('get_series'))

    def series_info(self, series_id) -> dict:
        data = self._get('get_series_info', series_id=series_id)
        if not isinstance(data, dict):
            return {'info': {}, 'episodes': {}}
        episodes = data.get('episodes') or {}
        # Sommige providers geven een lijst met lijsten i.p.v. een dict per seizoen.
        if isinstance(episodes, list):
            grouped = {}
            for item in episodes:
                for ep in (item if isinstance(item, list) else [item]):
                    if isinstance(ep, dict):
                        grouped.setdefault(str(ep.get('season', 1)), []).append(ep)
            episodes = grouped
        data['episodes'] = episodes
        data['info'] = data.get('info') if isinstance(data.get('info'), dict) else {}
        return data

    def episode_url(self, episode_id, ext) -> str:
        return f'{self.server}/series/{self.username}/{self.password}/{episode_id}.{ext}'

    # -- movies --------------------------------------------------------------
    def movie_categories(self):
        return _as_list(self._get('get_vod_categories'))

    def movies(self):
        return _as_list(self._get('get_vod_streams'))

    def movie_url(self, movie_id, ext) -> str:
        return f'{self.server}/movie/{self.username}/{self.password}/{movie_id}.{ext}'

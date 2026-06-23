#!/usr/bin/env python3
"""
IPTV Series Downloader
Downloads series from Xtream IPTV providers with bulk season/series support.
"""

import os
import re
import json
import sys
import subprocess
import argparse
from pathlib import Path
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt

console = Console()

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))

CONNECTION_TIMEOUT = 10
READ_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Strip characters that are illegal in filenames."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "Unknown"


def format_episode_filename(show_name: str, season: int, episode: int, title: str, ext: str) -> str:
    show = sanitize_filename(show_name)
    title_part = f" - {sanitize_filename(title)}" if title and title.strip() else ""
    return f"{show} - S{season:02d}E{episode:02d}{title_part}.{ext}"


def parse_m3u_plus_url(url: str) -> Optional[tuple]:
    """Extract (server, username, password) from an M3U+ get.php URL."""
    m = re.match(r'(https?://[^/]+)/get\.php\?username=([^&]+)&password=([^&]+)', url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


# ---------------------------------------------------------------------------
# Xtream API client
# ---------------------------------------------------------------------------

class XtreamClient:
    def __init__(self, server: str, username: str, password: str):
        self.server = server.rstrip('/')
        self.username = username
        self.password = password
        self._session = requests.Session()
        self._session.headers['User-Agent'] = 'IPTV-Downloader/1.0'
        self._base = {'username': username, 'password': password}

    def _get(self, action: str, extra: dict = None) -> any:
        params = {**self._base}
        if action:
            params['action'] = action
        if extra:
            params.update(extra)
        resp = self._session.get(
            f"{self.server}/player_api.php",
            params=params,
            timeout=(CONNECTION_TIMEOUT, READ_TIMEOUT)
        )
        resp.raise_for_status()
        return resp.json()

    def authenticate(self) -> bool:
        try:
            data = self._get('')
            return data.get('user_info', {}).get('auth', 0) == 1
        except Exception:
            return False

    def get_series_categories(self) -> list:
        return self._get('get_series_categories') or []

    def get_series(self, category_id: str = None) -> list:
        extra = {'category_id': category_id} if category_id else None
        return self._get('get_series', extra) or []

    def get_series_info(self, series_id) -> dict:
        return self._get('get_series_info', {'series_id': series_id}) or {}

    def stream_url(self, episode_id, ext: str) -> str:
        return f"{self.server}/series/{self.username}/{self.password}/{episode_id}.{ext}"


# ---------------------------------------------------------------------------
# Account persistence
# ---------------------------------------------------------------------------

CONFIG_FILE = CONFIG_DIR / "accounts.json"


def load_accounts() -> list:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return []


def save_accounts(accounts: list):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)


def add_account_interactive() -> Optional[dict]:
    console.print("\n[bold cyan]Add Account[/bold cyan]")
    console.print("1. Server / username / password")
    console.print("2. M3U+ URL (auto-extract credentials)")
    method = Prompt.ask("Method", choices=["1", "2"], default="1")

    if method == "2":
        url = Prompt.ask("M3U+ URL")
        result = parse_m3u_plus_url(url)
        if not result:
            console.print("[red]Could not parse URL — expected format: http://host/get.php?username=X&password=Y[/red]")
            return None
        server, username, password = result
    else:
        server   = Prompt.ask("Server URL (e.g. http://example.com:8080)")
        username = Prompt.ask("Username")
        password = Prompt.ask("Password", password=True)

    default_name = f"{username}@{server}"
    name = Prompt.ask("Label for this account", default=default_name)

    console.print("[yellow]Testing connection…[/yellow]")
    client = XtreamClient(server, username, password)
    if not client.authenticate():
        console.print("[red]Authentication failed. Check credentials and server URL.[/red]")
        return None

    console.print("[green]Connected![/green]")
    account = {'name': name, 'server': server, 'username': username, 'password': password}
    accounts = load_accounts()
    accounts.append(account)
    save_accounts(accounts)
    return account


def select_account() -> Optional[dict]:
    accounts = load_accounts()
    if not accounts:
        console.print("[yellow]No saved accounts.[/yellow]")
        return add_account_interactive()

    console.print("\n[bold cyan]Select Account[/bold cyan]")
    for i, acc in enumerate(accounts, 1):
        console.print(f"  {i}. {acc['name']}")
    console.print(f"  {len(accounts)+1}. Add new account")

    choice = IntPrompt.ask("Choice", default=1)
    if choice == len(accounts) + 1:
        return add_account_interactive()
    if 1 <= choice <= len(accounts):
        return accounts[choice - 1]
    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_episode(client: XtreamClient, episode: dict, show_name: str, out_dir: Path) -> bool:
    season_num = int(episode.get('season', 1))
    ep_num     = int(episode.get('episode_num', 1))
    title      = episode.get('title', '') or ''
    ext        = episode.get('container_extension', 'mkv')
    ep_id      = episode.get('id')

    filename = format_episode_filename(show_name, season_num, ep_num, title, ext)
    filepath = out_dir / filename

    if filepath.exists():
        console.print(f"  [dim]SKIP[/dim]  {filename} (already exists)")
        return True

    url = client.stream_url(ep_id, ext)
    console.print(f"  [green]↓[/green]    {filename}")

    cmd = [
        'ffmpeg',
        '-hide_banner', '-loglevel', 'error',
        '-i', url,
        '-c', 'copy',
        '-y',
        str(filepath)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr_snippet = result.stderr.strip()[:300]
            console.print(f"  [red]FAIL[/red]  {filename}\n        {stderr_snippet}")
            # Remove incomplete file
            if filepath.exists():
                filepath.unlink()
            return False
        return True
    except FileNotFoundError:
        console.print("[red]ffmpeg not found. Run inside the Docker container.[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        if filepath.exists():
            filepath.unlink()
        raise


# ---------------------------------------------------------------------------
# Interactive UI
# ---------------------------------------------------------------------------

def _print_series_table(series_list: list, limit: int = 60):
    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column("#", width=5, justify="right")
    table.add_column("Series Name")
    table.add_column("Rating", width=8)

    for i, s in enumerate(series_list[:limit], 1):
        table.add_row(str(i), s.get('name', 'Unknown'), str(s.get('rating', '') or ''))

    console.print(table)
    if len(series_list) > limit:
        console.print(f"[dim]Showing first {limit} of {len(series_list)} results.[/dim]")


def _pick_series(series_list: list) -> Optional[dict]:
    if not series_list:
        console.print("[yellow]No series found.[/yellow]")
        return None

    _print_series_table(series_list)
    raw = Prompt.ask("Select number (0 = back)", default="0")
    try:
        idx = int(raw)
        if 1 <= idx <= min(len(series_list), 60):
            return series_list[idx - 1]
    except ValueError:
        pass
    return None


def browse_categories(client: XtreamClient):
    with console.status("[yellow]Loading categories…[/yellow]"):
        cats = client.get_series_categories()

    if not cats:
        console.print("[red]No series categories found.[/red]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", width=5, justify="right")
    table.add_column("Category")
    for i, c in enumerate(cats, 1):
        table.add_row(str(i), c.get('category_name', 'Unknown'))
    console.print(table)

    raw = Prompt.ask("Select category (0 = back)", default="0")
    try:
        idx = int(raw)
        if 1 <= idx <= len(cats):
            cat = cats[idx - 1]
            with console.status(f"[yellow]Loading '{cat['category_name']}'…[/yellow]"):
                series_list = client.get_series(cat['category_id'])
            series = _pick_series(series_list)
            if series:
                handle_series(client, series)
    except ValueError:
        pass


def search_series(client: XtreamClient):
    query = Prompt.ask("Search").strip().lower()
    if not query:
        return

    with console.status("[yellow]Loading all series…[/yellow]"):
        all_series = client.get_series()

    results = [s for s in all_series if query in s.get('name', '').lower()]
    series = _pick_series(results)
    if series:
        handle_series(client, series)


def handle_series(client: XtreamClient, series: dict):
    show_name = series.get('name', 'Unknown Show')
    series_id = series.get('series_id')

    console.print(f"\n[bold]{show_name}[/bold]")
    with console.status("[yellow]Loading episode data…[/yellow]"):
        info = client.get_series_info(series_id)

    episodes_by_season: dict = info.get('episodes', {})
    if not episodes_by_season:
        console.print("[yellow]No episodes found.[/yellow]")
        return

    seasons = sorted(episodes_by_season.keys(), key=lambda x: int(x))

    console.print("\n[bold cyan]Seasons:[/bold cyan]")
    console.print("  0. All seasons")
    for i, s in enumerate(seasons, 1):
        count = len(episodes_by_season[s])
        console.print(f"  {i}. Season {s}  ({count} episode{'s' if count != 1 else ''})")

    raw = Prompt.ask("Select season (b = back)", default="b")
    if raw.lower() == 'b':
        return

    try:
        idx = int(raw)
    except ValueError:
        return

    if idx == 0:
        selected_keys = seasons
    elif 1 <= idx <= len(seasons):
        selected_keys = [seasons[idx - 1]]
    else:
        return

    episodes = []
    for k in selected_keys:
        episodes.extend(episodes_by_season[k])

    total = len(episodes)
    console.print(f"\n[cyan]{total} episode(s) selected.[/cyan]")

    # Output directory
    default_dir = DOWNLOAD_DIR / sanitize_filename(show_name)
    raw_dir = Prompt.ask("Download to", default=str(default_dir))
    out_dir = Path(raw_dir)

    if not Confirm.ask(f"Download {total} episode(s) to [cyan]{out_dir}[/cyan]?", default=True):
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    console.print()
    ok = fail = 0
    try:
        for ep in episodes:
            if download_episode(client, ep, show_name, out_dir):
                ok += 1
            else:
                fail += 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")

    console.print(f"\n[bold]Done:[/bold] [green]{ok} downloaded[/green]  [red]{fail} failed[/red]")


def main_menu(client: XtreamClient) -> bool:
    """Returns False when the user wants to switch account, True on quit."""
    while True:
        console.print("\n[bold cyan]Main Menu[/bold cyan]")
        console.print("  1. Browse by category")
        console.print("  2. Search series")
        console.print("  3. Switch account")
        console.print("  4. Quit")

        choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="1")
        if choice == "4":
            return True
        if choice == "3":
            return False
        if choice == "1":
            browse_categories(client)
        elif choice == "2":
            search_series(client)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="IPTV Series Downloader — bulk download seasons from Xtream IPTV"
    )
    parser.add_argument('--server',   help='Xtream server URL')
    parser.add_argument('--username', help='Username')
    parser.add_argument('--password', help='Password')
    parser.add_argument('--m3u-url',  help='M3U+ URL (extracts credentials automatically)')
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]IPTV Series Downloader[/bold cyan]\n[dim]Bulk download series & seasons from Xtream IPTV[/dim]",
        border_style="cyan"
    ))

    # CLI-supplied credentials
    if args.m3u_url:
        result = parse_m3u_plus_url(args.m3u_url)
        if not result:
            console.print("[red]Cannot parse M3U+ URL.[/red]")
            sys.exit(1)
        args.server, args.username, args.password = result

    if args.server and args.username and args.password:
        client = XtreamClient(args.server, args.username, args.password)
        console.print("[yellow]Connecting…[/yellow]")
        if not client.authenticate():
            console.print("[red]Authentication failed.[/red]")
            sys.exit(1)
        console.print("[green]Connected![/green]")
        main_menu(client)
        return

    # Interactive account flow
    while True:
        account = select_account()
        if not account:
            sys.exit(1)

        client = XtreamClient(account['server'], account['username'], account['password'])
        console.print("[yellow]Connecting…[/yellow]")
        if not client.authenticate():
            console.print("[red]Authentication failed.[/red]")
            continue

        console.print("[green]Connected![/green]")
        if main_menu(client):
            break


if __name__ == '__main__':
    main()

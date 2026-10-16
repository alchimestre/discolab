"""Last.fm as a seed provider.

Two things come back from it that the local files cannot give: how often a
track has actually been played, and the listening sessions those plays fall
into. A session is the container that turns listening into artist co-occurrence.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..model import RawTrack
from .base import ScanResult

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
PAGE_SIZE = 200
REQUEST_INTERVAL = 0.25
MAX_PAGES = 500


class LastfmError(RuntimeError):
    pass


class LastfmProvider:
    """Reads a user's scrobble history. Needs an API key, not the shared secret."""

    name = "lastfm"

    def __init__(
        self,
        api_key: str,
        username: str,
        recent_limit: int = 20000,
        session_gap_minutes: int = 30,
        top_pages: int = 25,
    ):
        self.api_key = api_key
        self.username = username
        self.recent_limit = recent_limit
        self.session_gap = timedelta(minutes=session_gap_minutes)
        self.top_pages = top_pages

    def scan(self) -> ScanResult:
        plays = self._top_track_plays()
        scrobbles, skipped = self._recent_scrobbles()

        sessions = _sessions(scrobbles, self.session_gap)
        containers: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for name, members in sessions:
            for artist, title in members:
                containers[(artist, title)].append(("session", name))

        seen: set[tuple[str, str]] = set()
        tracks: list[RawTrack] = []
        for artist, title in list(plays) + [key for key in containers if key not in plays]:
            if (artist, title) in seen:
                continue
            seen.add((artist, title))
            tracks.append(
                RawTrack(
                    source=self.name,
                    path="",
                    artist_raw=artist,
                    title_raw=title,
                    album_raw=None,
                    duration_ms=None,
                    containers=tuple(containers.get((artist, title), ())),
                    plays=plays.get((artist, title), 1),
                )
            )
        return ScanResult(source=self.name, tracks=tuple(tracks), skipped=tuple(skipped))

    def _call(self, method: str, **params) -> dict:
        query = {"method": method, "api_key": self.api_key, "format": "json", "user": self.username}
        query.update(params)
        url = API_ROOT + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"User-Agent": "discolab/0.1"})
        time.sleep(REQUEST_INTERVAL)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as error:
            raise LastfmError(f"{method}: {error}") from error
        if "error" in payload:
            raise LastfmError(f"{method}: {payload['error']} {payload.get('message', '')}")
        return payload

    def _top_track_plays(self) -> dict[tuple[str, str], int]:
        """Total plays per track, which the recent history alone undercounts."""
        plays: dict[tuple[str, str], int] = {}
        for page in range(1, self.top_pages + 1):
            payload = self._call(
                "user.gettoptracks", limit=PAGE_SIZE, page=page, period="overall"
            )
            batch = payload.get("toptracks", {}).get("track", [])
            if not batch:
                break
            for track in batch:
                artist = track.get("artist", {}).get("name", "")
                name = track.get("name", "")
                if artist and name:
                    plays[(artist, name)] = int(track.get("playcount", 0))
            if len(batch) < PAGE_SIZE:
                break
        return plays

    def _recent_scrobbles(self) -> tuple[list[tuple[datetime, str, str]], list[tuple[str, str]]]:
        """Scrobbles newest first, with a timestamp, for building sessions."""
        scrobbles: list[tuple[datetime, str, str]] = []
        skipped: list[tuple[str, str]] = []
        for page in range(1, MAX_PAGES + 1):
            payload = self._call("user.getrecenttracks", limit=PAGE_SIZE, page=page)
            batch = payload.get("recenttracks", {}).get("track", [])
            if not batch:
                break
            for track in batch:
                artist = track.get("artist", {}).get("#text", "")
                name = track.get("name", "")
                stamp = track.get("date", {}).get("uts")
                if stamp is None:
                    # The now-playing entry has no timestamp and is not history.
                    continue
                if not artist or not name:
                    skipped.append((f"{artist} - {name}", "missing artist or title"))
                    continue
                scrobbles.append((datetime.fromtimestamp(int(stamp), tz=timezone.utc), artist, name))
            if len(scrobbles) >= self.recent_limit or len(batch) < PAGE_SIZE:
                break
        scrobbles.sort(key=lambda row: row[0])
        return scrobbles, skipped


def _sessions(
    scrobbles: list[tuple[datetime, str, str]], gap: timedelta
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Group scrobbles into listening sessions split by a gap in playback."""
    sessions: list[tuple[str, list[tuple[str, str]]]] = []
    current: list[tuple[str, str]] = []
    start: datetime | None = None
    previous: datetime | None = None

    for stamp, artist, title in scrobbles:
        if previous is not None and stamp - previous > gap:
            if current and start is not None:
                sessions.append((_session_name(start), current))
            current = []
            start = None
        if start is None:
            start = stamp
        current.append((artist, title))
        previous = stamp

    if current and start is not None:
        sessions.append((_session_name(start), current))
    return sessions


def _session_name(start: datetime) -> str:
    return start.strftime("%Y-%m-%d %H:%M")

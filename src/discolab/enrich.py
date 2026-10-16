"""Enrich artists from an API that answers fast.

Deezer supplies genres and label names. The alternatives were measured and both
rejected: Discogs allows sixty requests a minute, which turns the whole graph
into an hour of waiting, and iTunes throttles to thirty-two successes per three
hundred requests. MusicBrainz is accurate but allows one request a second.

Deezer's own limit is the reason this module is not simply concurrent. Overrun it
and it still answers HTTP 200, with the refusal in the body:

    {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}

Treated as an ordinary reply that error reads as "this artist has no genres",
which is how a rate limit gets written into the graph as a fact. So the request
rate is paced below the limit, and a refusal is raised rather than returned.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEEZER_ROOT = "https://api.deezer.com"
DEEZER_AGENT = "discolab/0.1 +local"
DEEZER_ALBUMS_SAMPLED = 3
QUOTA_CODES = {4, 700}
DEEZER_RETRIES = 4

PLACEHOLDER_LABEL = re.compile(
    r"records dk|tune core|tunecore|cd baby|distrokid|amuse io|routenote|"
    r"believe digital|the orchard|create music group",
    re.IGNORECASE,
)


class DeezerError(RuntimeError):
    """Deezer refused the request, so nothing can be concluded about the artist."""


@dataclass(frozen=True, slots=True)
class ReleaseFacts:
    genres: tuple[str, ...]
    labels: tuple[str, ...]


def _name_key(name: str) -> str:
    """Fold a name for comparison, keeping letters and digits only."""
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


class _RateLimiter:
    """Hand out slots so a pool of workers cannot outrun the quota together.

    Every worker waits here before a request, which is what keeps the total rate
    under Deezer's limit instead of discovering the limit by being refused.
    """

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            time.sleep(delay)


class DeezerEnricher:
    """Genres and labels for an artist, paced to stay inside the quota.

    The route matters. Searching albums matches compilations and soundtracks
    that merely share a name, which is how Rome came back as Films and Video
    Games. Asking for the artist, then that artist's own albums, returns the
    real labels: Warp for Squarepusher, Dekmantel for Polygonia, MORD for Quelza.
    """

    def __init__(
        self,
        agent: str = DEEZER_AGENT,
        timeout: float = 15.0,
        rate: float = 8.0,
    ):
        self.agent = agent
        self.timeout = timeout
        self.requests = 0
        self.limiter = _RateLimiter(rate)

    def facts(self, artist: str) -> ReleaseFacts | None:
        found = self._get("/search/artist", {"q": artist, "limit": 1})
        items = found.get("data") or []
        if not items:
            return None
        match = items[0]
        # Deezer answers a near miss with a different artist: Rome resolves to
        # Romé. Anything but the name itself is the wrong artist.
        if _name_key(match.get("name", "")) != _name_key(artist):
            return None

        albums = self._get(
            f"/artist/{match['id']}/albums", {"limit": DEEZER_ALBUMS_SAMPLED}
        )
        albums = albums.get("data") or []
        if not albums:
            return ReleaseFacts(genres=(), labels=())

        genres: set[str] = set()
        labels: set[str] = set()
        for album in albums[:DEEZER_ALBUMS_SAMPLED]:
            detail = self._get(f"/album/{album['id']}")
            for genre in (detail.get("genres") or {}).get("data", []):
                if genre.get("name"):
                    genres.add(genre["name"])
            label = (detail.get("label") or "").strip()
            if label and not PLACEHOLDER_LABEL.search(label):
                labels.add(label)
        return ReleaseFacts(genres=tuple(sorted(genres)), labels=tuple(sorted(labels)))

    def _get(self, path: str, params: dict | None = None) -> dict:
        """One request, paced, retried while Deezer is refusing.

        Raises DeezerError when the request cannot be completed, so a refusal is
        never mistaken for an artist with no genres.
        """
        url = DEEZER_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": self.agent})

        last = ""
        for attempt in range(DEEZER_RETRIES):
            self.limiter.wait()
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as error:
                if error.code == 429 or error.code >= 500:
                    last = f"HTTP {error.code}"
                    time.sleep(2 ** attempt)
                    continue
                raise DeezerError(f"HTTP {error.code} for {path}") from error
            except Exception as error:
                last = f"{type(error).__name__}"
                time.sleep(2 ** attempt)
                continue

            if "error" in payload:
                error = payload["error"]
                code = error.get("code")
                if code in QUOTA_CODES or error.get("type") == "Exception":
                    last = f"quota {code}"
                    time.sleep(2 ** attempt)
                    continue
                raise DeezerError(f"{error} for {path}")
            return payload

        raise DeezerError(f"{last} after {DEEZER_RETRIES} attempts for {path}")

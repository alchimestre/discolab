"""Enrich artists from an API that answers fast.

Deezer supplies genres and label names. The alternatives were measured and both
rejected: Discogs allows sixty requests a minute, which turns the whole graph
into an hour of waiting, and iTunes throttles to thirty-two successes per three
hundred requests. MusicBrainz is accurate but allows one request a second.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEEZER_ROOT = "https://api.deezer.com"
DEEZER_AGENT = "discolab/0.1 +local"
DEEZER_ALBUMS_SAMPLED = 3

# A DistroKid or CD Baby placeholder is not a label anybody curates.
PLACEHOLDER_LABEL = re.compile(
    r"records dk|tune core|tunecore|cd baby|distrokid|amuse io|routenote|"
    r"believe digital|the orchard|create music group",
    re.IGNORECASE,
)


class DeezerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseFacts:
    genres: tuple[str, ...]
    labels: tuple[str, ...]


def _name_key(name: str) -> str:
    """Fold a name for comparison, keeping letters and digits only."""
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


class DeezerEnricher:
    """Genres and labels for an artist, thousands of requests a minute.

    The route matters. Searching albums matches compilations and soundtracks
    that merely share a name, which is how Rome came back as Films and Video
    Games. Asking for the artist, then that artist's own albums, returns the
    real labels: Warp for Squarepusher, Dekmantel for Polygonia, MORD for
    Quelza.
    """

    def __init__(self, agent: str = DEEZER_AGENT, timeout: float = 15.0):
        self.agent = agent
        self.timeout = timeout

    def facts(self, artist: str) -> ReleaseFacts | None:
        found = self._get("/search/artist", {"q": artist, "limit": 1})
        items = (found or {}).get("data") or []
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
        albums = (albums or {}).get("data") or []
        if not albums:
            return ReleaseFacts(genres=(), labels=())

        genres: set[str] = set()
        labels: set[str] = set()
        for album in albums[:DEEZER_ALBUMS_SAMPLED]:
            detail = self._get(f"/album/{album['id']}")
            if not detail:
                continue
            for genre in (detail.get("genres") or {}).get("data", []):
                if genre.get("name"):
                    genres.add(genre["name"])
            label = (detail.get("label") or "").strip()
            if label and not PLACEHOLDER_LABEL.search(label):
                labels.add(label)
        return ReleaseFacts(genres=tuple(sorted(genres)), labels=tuple(sorted(labels)))

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        url = DEEZER_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": self.agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            if error.code == 429:
                time.sleep(2)
            return None
        except Exception:
            return None

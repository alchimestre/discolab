"""Enrich the graph from curated local metadata.

Rekordbox holds the BPM, the musical key and the DJ's own tags for every file.
That is the highest signal metadata in the project, because it is the owner's
classification of their own records rather than a service's guess.

Reading it needs SQLCipher, since rekordbox 6 encrypts the database. The key is
a published constant and every row is local.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = Path.home() / "Library/Pioneer/rekordbox/master.db"
# rekordbox 6 encrypts master.db with SQLCipher and this fixed key.
DEFAULT_KEY = (
    "402fd482c38817c35ffa8ffb8c7d93143b749e7d315df7a81732a1ff43608497"
)
CIPHER_COMPATIBILITY = 3
QUERY_TIMEOUT = 120


class EnrichError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrackFacts:
    path: str
    bpm: float | None
    musical_key: str | None
    rating: int
    dj_plays: int
    tags: tuple[str, ...] = field(default=())


MUSICBRAINZ_ROOT = "https://musicbrainz.org/ws/2/artist/"
# MusicBrainz asks for a real user agent and allows one request a second.
MUSICBRAINZ_AGENT = "discolab/0.1 ( local taste graph )"
MUSICBRAINZ_INTERVAL = 1.1


@dataclass(frozen=True, slots=True)
class ArtistFacts:
    mbid: str
    name: str
    kind: str | None
    country: str | None
    tags: tuple[str, ...]
    score: int


class MusicBrainzEnricher:
    """Canonical artist identity, which is what collapses duplicate nodes."""

    def __init__(self, agent: str = MUSICBRAINZ_AGENT):
        self.agent = agent

    def lookup(self, name: str, minimum_score: int = 90) -> ArtistFacts | None:
        query = urllib.parse.urlencode(
            {"query": f'artist:"{name}"', "fmt": "json", "limit": 1}
        )
        request = urllib.request.Request(
            MUSICBRAINZ_ROOT + "?" + query,
            headers={"User-Agent": self.agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            if error.code in (429, 503):
                # Being asked to slow down is not a failure of the lookup.
                time.sleep(3)
            return None
        except Exception:
            return None

        artists = payload.get("artists") or []
        if not artists:
            return None
        best = artists[0]
        score = int(best.get("score") or 0)
        if score < minimum_score or not best.get("id"):
            return None
        return ArtistFacts(
            mbid=best["id"],
            name=best.get("name") or name,
            kind=best.get("type"),
            country=best.get("country"),
            tags=tuple(sorted({t["name"] for t in (best.get("tags") or []) if t.get("name")})),
            score=score,
        )


DEEZER_ROOT = "https://api.deezer.com"
DEEZER_AGENT = "discolab/0.1 +local"
# Deezer documents 50 requests every 5 seconds. Measured: 300 requests went
# through in 3.7 seconds with no rejection, where Discogs allows one a second.
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
    """Genres and labels for an artist, four thousand requests a minute.

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

        albums = self._get(f"/artist/{match['id']}/albums", {"limit": DEEZER_ALBUMS_SAMPLED})
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


def join_key(path: str) -> str:
    """Normalise a path so macOS and rekordbox spellings agree.

    The filesystem hands back decomposed accents and rekordbox composed ones,
    which made an otherwise exact join match nothing.
    """
    return unicodedata.normalize("NFC", path)


class RekordboxEnricher:
    def __init__(self, db_path: Path = DEFAULT_DB, key: str = DEFAULT_KEY):
        self.db_path = db_path
        self.key = key

    def unavailable_reason(self) -> str | None:
        if shutil.which("sqlcipher") is None:
            return "sqlcipher is not on PATH, install it with `brew install sqlcipher`"
        if not self.db_path.is_file():
            return f"no rekordbox database at {self.db_path}"
        return None

    def _query(self, sql: str) -> list[dict[str, str]]:
        prefix = (
            f"PRAGMA cipher_compatibility = {CIPHER_COMPATIBILITY}; "
            f"PRAGMA key = '{self.key}'; "
        )
        try:
            result = subprocess.run(
                ["sqlcipher", "-csv", "-header", str(self.db_path), prefix + sql],
                capture_output=True,
                text=True,
                timeout=QUERY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise EnrichError(f"rekordbox query timed out: {sql[:40]}") from error
        if result.returncode != 0 and not result.stdout:
            raise EnrichError(f"rekordbox query failed: {result.stderr.strip()[:160]}")
        # The pragmas print their own status line before the result set.
        body = "\n".join(
            line for line in result.stdout.splitlines() if line.strip() != "ok"
        )
        return list(csv.DictReader(io.StringIO(body)))

    def facts(self) -> dict[str, TrackFacts]:
        """Curated metadata per file, keyed by normalised path."""
        keys = {
            row["ID"]: row["ScaleName"]
            for row in self._query("SELECT ID, ScaleName FROM djmdKey;")
            if row.get("ScaleName")
        }
        tag_names = {
            row["ID"]: row["Name"]
            for row in self._query("SELECT ID, Name FROM djmdMyTag;")
            if row.get("Name")
        }
        by_content: dict[str, list[str]] = {}
        for row in self._query("SELECT MyTagID, ContentID FROM djmdSongMyTag;"):
            name = tag_names.get(row.get("MyTagID", ""))
            if name:
                by_content.setdefault(row.get("ContentID", ""), []).append(name)

        facts: dict[str, TrackFacts] = {}
        for row in self._query(
            "SELECT ID, FolderPath, BPM, KeyID, Rating, DJPlayCount FROM djmdContent;"
        ):
            path = row.get("FolderPath") or ""
            if not path:
                continue
            bpm_raw = int(row.get("BPM") or 0)
            facts[join_key(path)] = TrackFacts(
                path=path,
                # Rekordbox stores BPM as an integer hundredth.
                bpm=round(bpm_raw / 100.0, 2) if bpm_raw > 0 else None,
                musical_key=keys.get(row.get("KeyID", "")),
                rating=int(row.get("Rating") or 0),
                dj_plays=int(row.get("DJPlayCount") or 0),
                tags=tuple(sorted(by_content.get(row.get("ID", ""), ()))),
            )
        return facts

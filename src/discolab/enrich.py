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
import shutil
import subprocess
import unicodedata
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

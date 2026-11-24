from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager

from ..config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist (
    key TEXT PRIMARY KEY,
    name TEXT,
    external INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    styles TEXT NOT NULL DEFAULT '[]',
    labels TEXT NOT NULL DEFAULT '[]',
    lastfm INTEGER NOT NULL DEFAULT 0,
    community INTEGER NOT NULL DEFAULT -1,
    component INTEGER NOT NULL DEFAULT -1,
    component_size INTEGER NOT NULL DEFAULT 0,
    bridge_score REAL NOT NULL DEFAULT 0,
    degree INTEGER NOT NULL DEFAULT 0,
    area INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS track (
    key TEXT PRIMARY KEY,
    title TEXT,
    artist_key TEXT NOT NULL,
    duration_ms INTEGER,
    paths TEXT NOT NULL DEFAULT '[]',
    plays INTEGER NOT NULL DEFAULT 0,
    bpm REAL,
    musical_key TEXT,
    rating INTEGER,
    dj_plays INTEGER
);
CREATE INDEX IF NOT EXISTS track_artist ON track (artist_key);
CREATE TABLE IF NOT EXISTS container (key TEXT PRIMARY KEY, kind TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS contains (
    container_key TEXT NOT NULL, track_key TEXT NOT NULL,
    PRIMARY KEY (container_key, track_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS credited (
    artist_key TEXT NOT NULL, track_key TEXT NOT NULL, role TEXT NOT NULL,
    PRIMARY KEY (artist_key, track_key, role)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS similar (
    seed_key TEXT NOT NULL, artist_key TEXT NOT NULL, score REAL, source TEXT,
    PRIMARY KEY (seed_key, artist_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS similar_artist ON similar (artist_key);
CREATE TABLE IF NOT EXISTS collaborates (
    a TEXT NOT NULL, b TEXT NOT NULL, weight INTEGER NOT NULL,
    PRIMARY KEY (a, b)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS co_occurs (
    a TEXT NOT NULL, b TEXT NOT NULL, weight INTEGER NOT NULL,
    PRIMARY KEY (a, b)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS track_tag (
    track_key TEXT NOT NULL, tag TEXT NOT NULL,
    PRIMARY KEY (track_key, tag)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS artist_tag (
    artist_key TEXT NOT NULL, tag TEXT NOT NULL, tracks INTEGER NOT NULL,
    PRIMARY KEY (artist_key, tag)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS area (id INTEGER PRIMARY KEY, region INTEGER NOT NULL);
"""

TABLES = (
    "artist",
    "track",
    "container",
    "contains",
    "credited",
    "similar",
    "collaborates",
    "co_occurs",
    "track_tag",
    "artist_tag",
    "area",
)

# An artist no table refers to. Pair tables store one row per unordered pair,
# so both columns are checked.
ORPHAN = """
    NOT EXISTS (SELECT 1 FROM track WHERE artist_key = artist.key)
    AND NOT EXISTS (SELECT 1 FROM credited WHERE artist_key = artist.key)
    AND NOT EXISTS (SELECT 1 FROM artist_tag WHERE artist_key = artist.key)
    AND NOT EXISTS (SELECT 1 FROM similar WHERE seed_key = artist.key)
    AND NOT EXISTS (SELECT 1 FROM similar WHERE artist_key = artist.key)
    AND NOT EXISTS (SELECT 1 FROM collaborates WHERE a = artist.key OR b = artist.key)
    AND NOT EXISTS (SELECT 1 FROM co_occurs WHERE a = artist.key OR b = artist.key)
"""


@contextmanager
def connect(settings: Settings) -> Generator[sqlite3.Connection]:
    """Open the database file, creating it and its tables on first use."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


def keys_json(keys: Sequence[str]) -> str:
    """A key list as one parameter, read back with `json_each`."""
    return json.dumps(list(keys))


def orphan_count(connection: sqlite3.Connection) -> int:
    return connection.execute(f"SELECT count(*) FROM artist WHERE {ORPHAN}").fetchone()[0]


def delete_orphans(connection: sqlite3.Connection) -> int:
    """Remove artists with no tracks, credits, tags or edges."""
    return connection.execute(f"DELETE FROM artist WHERE {ORPHAN}").rowcount


def clear(connection: sqlite3.Connection) -> None:
    for table in TABLES:
        connection.execute(f"DELETE FROM {table}")

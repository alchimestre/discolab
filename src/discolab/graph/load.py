from __future__ import annotations

import json
import sqlite3

from ..model import Catalog


def load_catalog(connection: sqlite3.Connection, catalog: Catalog) -> None:
    connection.executemany(
        "INSERT INTO artist (key, name) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET name = excluded.name",
        [(artist.key, artist.name) for artist in catalog.artists],
    )
    connection.executemany(
        "INSERT INTO container (key, kind, name) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET kind = excluded.kind, name = excluded.name",
        [(container.key, container.kind, container.name) for container in catalog.containers],
    )
    connection.executemany(
        """
        INSERT INTO track (key, title, artist_key, duration_ms, paths, plays)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET
            title = excluded.title, artist_key = excluded.artist_key,
            duration_ms = excluded.duration_ms, paths = excluded.paths,
            plays = excluded.plays
        """,
        [
            (
                track.key,
                track.title,
                track.artist_key,
                track.duration_ms,
                json.dumps(track.paths),
                track.plays,
            )
            for track in catalog.tracks
        ],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO contains (container_key, track_key) VALUES (?, ?)",
        catalog.memberships,
    )
    connection.executemany(
        "INSERT OR IGNORE INTO credited (artist_key, track_key, role) VALUES (?, ?, ?)",
        [(credit.artist_key, credit.track_key, credit.role) for credit in catalog.credits],
    )


def write_lastfm_tags(connection: sqlite3.Connection, rows: list[dict]) -> int:
    """Attach Last.fm's tags where no genre source has described the artist.

    Styles are filled rather than replaced. Two taxonomies on one artist make a
    cluster's genre evidence say two things at once, and the count that decides a
    family would then be counting a blend of them. `lastfm` records that the
    artist was asked, so a later run does not ask again.
    """
    connection.executemany(
        """
        UPDATE artist
        SET styles = CASE WHEN styles = '[]' THEN ? ELSE styles END, lastfm = 1
        WHERE key = ?
        """,
        [(json.dumps(row["styles"]), row["key"]) for row in rows],
    )
    # Committed per batch: a run of twelve thousand requests is otherwise lost
    # whole when it is interrupted.
    connection.commit()
    return len(rows)


def write_track_facts(connection: sqlite3.Connection, rows: list[dict]) -> int:
    """Attach curated facts and their tags to tracks, then roll tags up to artists."""
    if not rows:
        return 0
    connection.executemany(
        "UPDATE track SET bpm = :bpm, musical_key = :musical_key, "
        "rating = :rating, dj_plays = :dj_plays WHERE key = :key",
        rows,
    )
    connection.executemany(
        "INSERT OR IGNORE INTO track_tag (track_key, tag) VALUES (?, ?)",
        [(row["key"], tag) for row in rows for tag in row["tags"]],
    )
    # An artist's genres are the union of what its tracks carry, weighted by
    # how many tracks carry each.
    connection.execute("DELETE FROM artist_tag")
    connection.execute(
        """
        INSERT INTO artist_tag (artist_key, tag, tracks)
        SELECT t.artist_key, g.tag, count(*)
        FROM track_tag g JOIN track t ON t.key = g.track_key
        GROUP BY t.artist_key, g.tag
        """
    )
    return len(rows)


def drop_single_seed_artists(connection: sqlite3.Connection) -> int:
    """Remove expansion artists that only one seed points at.

    A candidate one seed names and no other can never reach a page: the ranking
    needs several seeds to agree. Left in, they are most of the artists in the
    database and every later pass walks them. Measured at 30,832 of 43,906.
    """
    dropped = connection.execute(
        """
        DELETE FROM artist
        WHERE external = 1
          AND NOT EXISTS (SELECT 1 FROM track WHERE artist_key = artist.key)
          AND (SELECT count(*) FROM similar WHERE artist_key = artist.key) < 2
        """
    ).rowcount
    connection.execute(
        """
        DELETE FROM similar
        WHERE NOT EXISTS (SELECT 1 FROM artist WHERE key = similar.artist_key)
           OR NOT EXISTS (SELECT 1 FROM artist WHERE key = similar.seed_key)
        """
    )
    return dropped


def write_expansion(connection: sqlite3.Connection, rows: list[dict]) -> int:
    """Write similarity edges and the artists they point at.

    A suggestion that is already in the corpus still gets its edge: those are
    the links that pull an artist with no co-occurrence at all into the graph.
    Only genuinely new artists are marked external.
    """
    connection.executemany(
        """
        INSERT INTO artist (key, name, external, url)
        VALUES (:key, :name, :external, :url)
        ON CONFLICT (key) DO UPDATE SET url = coalesce(excluded.url, url)
        """,
        rows,
    )
    connection.executemany(
        """
        INSERT INTO similar (seed_key, artist_key, score, source)
        VALUES (:seed_key, :key, :score, :source)
        ON CONFLICT (seed_key, artist_key) DO UPDATE SET
            score = excluded.score, source = excluded.source
        """,
        rows,
    )
    # Committed per batch, so a twenty-minute expansion shows up as it goes.
    connection.commit()
    return len(rows)

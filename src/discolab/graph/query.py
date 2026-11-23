from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from .store import TABLES, keys_json

EDGE_TABLES = tuple(table for table in TABLES if table not in ("artist", "track", "container"))


def bridge_candidates(
    connection: sqlite3.Connection,
    limit: int = 20,
    min_degree: int = 2,
    min_bridge: float = 0.0,
) -> list[dict]:
    """Artists that sit on paths between otherwise separate parts of the graph."""
    return _dicts(
        connection.execute(
            """
            SELECT key, name, community, component, component_size, bridge_score, degree
            FROM artist
            WHERE degree >= ? AND bridge_score > ?
            ORDER BY bridge_score DESC, degree DESC
            LIMIT ?
            """,
            (min_degree, min_bridge, limit),
        )
    )


def owned_tracks_for(
    connection: sqlite3.Connection, artist_keys: list[str], per_artist: int = 5
) -> dict[str, list[dict]]:
    rows = connection.execute(
        """
        SELECT artist_key, title, coalesce(paths ->> 0, '') AS path, plays
        FROM track
        WHERE artist_key IN (SELECT value FROM json_each(?))
        ORDER BY plays DESC, title
        """,
        (keys_json(artist_keys),),
    )
    owned: dict[str, list[dict]] = {}
    for row in rows:
        tracks = owned.setdefault(row["artist_key"], [])
        if len(tracks) < per_artist:
            tracks.append({"title": row["title"], "path": row["path"], "plays": row["plays"]})
    return owned


def islands(connection: sqlite3.Connection, limit: int = 8, sample: int = 6) -> list[dict]:
    """Connected components of the artist graph, largest first.

    A component is a group of artists reachable from one another. Separate
    components have no edge between them, so nothing in the data relates them.
    """
    rows = connection.execute(
        """
        SELECT component, component_size AS size, name FROM artist
        WHERE component >= 0 AND component < ?
        ORDER BY component, degree DESC, name
        """,
        (limit,),
    )
    found: dict[int, dict] = {}
    for row in rows:
        island = found.setdefault(
            row["component"], {"component": row["component"], "size": row["size"], "members": []}
        )
        if len(island["members"]) < sample:
            island["members"].append(row["name"])
    return list(found.values())


def corpus_keys(connection: sqlite3.Connection) -> list[str]:
    """Keys of artists from the user's own data.

    Expansion output is excluded, or the second run would treat its own
    suggestions as things the user already has.
    """
    return [row[0] for row in connection.execute("SELECT key FROM artist WHERE external = 0")]


def seed_artists(
    connection: sqlite3.Connection,
    per_community: int = 3,
    min_degree: int = 2,
    isolated_limit: int = 0,
) -> list[dict]:
    """The most central members of each taste cluster, to expand from.

    Taking the top bridges alone concentrates the seeds in whichever island is
    largest. One artist per cluster keeps them spread across the taste, which
    is what makes two unrelated areas agreeing on a name mean something.

    An artist with no edges has no cluster and can never be reached by another
    seed, so expanding from them is the only way the metal record buried in the
    filesystem joins the rest of the metal. `isolated_limit` admits that many
    of them, best represented first.
    """
    seeds = _dicts(
        connection.execute(
            """
            SELECT key, name, community, degree, 0 AS isolated FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY community ORDER BY degree DESC, name
                ) AS position
                FROM artist
                WHERE external = 0 AND community >= 0 AND degree >= ?
            )
            WHERE position <= ?
            ORDER BY community, degree DESC
            """,
            (min_degree, per_community),
        )
    )
    lonely = connection.execute(
        """
        SELECT a.key, a.name, -1 AS community, 0 AS degree, 1 AS isolated
        FROM artist a JOIN track t ON t.artist_key = a.key
        WHERE a.external = 0 AND a.degree = 0
        GROUP BY a.key
        HAVING sum(t.paths <> '[]') > 0 OR sum(t.plays) > 0
        ORDER BY count(*) DESC, sum(t.plays) DESC
        LIMIT ?
        """,
        (isolated_limit,),
    )
    return seeds + _dicts(lonely)


def artist_sources(connection: sqlite3.Connection, keys: Sequence[str]) -> dict[str, dict]:
    """Styles and labels per artist key, for whatever has been fetched so far."""
    rows = connection.execute(
        "SELECT key, styles, labels FROM artist WHERE key IN (SELECT value FROM json_each(?))",
        (keys_json(keys),),
    )
    return {
        row["key"]: {
            "key": row["key"],
            "styles": json.loads(row["styles"]),
            "labels": json.loads(row["labels"]),
        }
        for row in rows
    }


def clusters(connection: sqlite3.Connection, limit: int = 40, sample: int = 6) -> list[dict]:
    """The taste clusters, largest first, with their members and usual styles."""
    found: dict[int, dict] = {}
    for row in connection.execute(
        """
        SELECT community, name, count(*) OVER (PARTITION BY community) AS size
        FROM artist
        WHERE community >= 0 AND external = 0
        ORDER BY size DESC, community, degree DESC, name
        """
    ):
        if row["community"] not in found and len(found) == limit:
            break
        entry = found.setdefault(
            row["community"],
            {"cluster": row["community"], "size": row["size"], "members": [], "styles": []},
        )
        if len(entry["members"]) < sample:
            entry["members"].append(row["name"])

    for row in connection.execute(
        """
        SELECT a.community, style.value AS style, count(*) AS n
        FROM artist a, json_each(a.styles) AS style
        WHERE a.community >= 0
        GROUP BY a.community, style.value
        ORDER BY n DESC, style.value
        """
    ):
        entry = found.get(row["community"])
        if entry is not None and len(entry["styles"]) < 5:
            entry["styles"].append(row["style"])
    return list(found.values())


def cluster_frontier(
    connection: sqlite3.Connection, cluster: int, limit: int = 25
) -> list[dict]:
    """Artists outside the corpus reached from one cluster's seeds.

    Scoping to a cluster answers a different question from the global ranking:
    not what the whole taste agrees on, but what this corner of it leads to.
    """
    rows = connection.execute(
        """
        SELECT e.key, e.name, count(*) AS seed_count, max(r.score) AS best_score,
               json_group_array(s.name) AS seed_names
        FROM similar r
        JOIN artist s ON s.key = r.seed_key
        JOIN artist e ON e.key = r.artist_key
        WHERE s.community = ? AND e.external = 1
        GROUP BY e.key
        ORDER BY best_score DESC, seed_count DESC
        LIMIT ?
        """,
        (cluster, limit),
    )
    return [{**dict(row), "seed_names": json.loads(row["seed_names"])[:4]} for row in rows]


def cluster_periphery(
    connection: sqlite3.Connection, cluster: int, limit: int = 25
) -> list[dict]:
    """The cluster's own artists that barely connect to it.

    A member with one edge sits at the edge of the taste rather than in it,
    which is where something recognisable but rarely played tends to live.
    """
    return _dicts(
        connection.execute(
            """
            SELECT key, name, community, degree, bridge_score FROM artist
            WHERE community = ? AND external = 0
            ORDER BY degree, name
            LIMIT ?
            """,
            (cluster, limit),
        )
    )


def track_paths(connection: sqlite3.Connection) -> list[dict]:
    return _dicts(
        connection.execute(
            "SELECT t.key, path.value AS path FROM track t, json_each(t.paths) AS path"
        )
    )


def artist_tempo(connection: sqlite3.Connection, keys: Sequence[str]) -> dict[str, dict]:
    """Tempo range and keys per artist, from the tracks the library holds."""
    rows = connection.execute(
        """
        SELECT artist_key AS key, count(*) AS tracks, min(bpm) AS low, max(bpm) AS high,
               avg(bpm) AS mean, json_group_array(DISTINCT musical_key) AS musical_keys
        FROM track
        WHERE bpm IS NOT NULL AND artist_key IN (SELECT value FROM json_each(?))
        GROUP BY artist_key
        """,
        (keys_json(keys),),
    )
    tempo = {}
    for row in rows:
        musical_keys = [key for key in json.loads(row["musical_keys"]) if key is not None]
        tempo[row["key"]] = {**dict(row), "musical_keys": musical_keys[:4]}
    return tempo


def external_candidates(
    connection: sqlite3.Connection,
    limit: int = 25,
    min_areas: int = 2,
    min_area_seeds: int = 5,
) -> list[dict]:
    """Artists from outside the corpus, ranked by the weaker of their two best areas.

    An area votes for an artist when at least two of its seeds name it, and its
    support is the share of the area's seeds that do. Ranking by the
    second-best support rewards an artist two separate tastes both lead to;
    ranking by seed count rewards the centre of the largest area. Areas with
    fewer than `min_area_seeds` seeds are ignored, since two seeds out of two
    is a full vote from almost nothing.

    There is no genre predicate. Narrowing happens by scoping to a cluster, so
    the graph stays complete and the filter is a choice about where to look
    rather than a value baked into the rank.
    """
    rows = _dicts(
        connection.execute(
            """
            WITH seed AS (
                SELECT DISTINCT r.seed_key AS key, s.area
                FROM similar r JOIN artist s ON s.key = r.seed_key
                WHERE s.area >= 0
            ),
            eligible AS (
                SELECT area, count(*) AS area_seeds FROM seed
                GROUP BY area HAVING count(*) >= :min_area_seeds
            ),
            vote AS (
                SELECT r.artist_key AS key, seed.area, count(*) AS votes,
                       1.0 * count(*) / eligible.area_seeds AS support
                FROM similar r
                JOIN seed ON seed.key = r.seed_key
                JOIN eligible ON eligible.area = seed.area
                JOIN artist e ON e.key = r.artist_key AND e.external = 1
                GROUP BY r.artist_key, seed.area
                HAVING count(*) >= 2
            ),
            ranked AS (
                SELECT key, area, support,
                       row_number() OVER (
                           PARTITION BY key ORDER BY support DESC, area
                       ) AS position,
                       count(*) OVER (PARTITION BY key) AS areas,
                       sum(votes) OVER (PARTITION BY key) AS seed_count
                FROM vote
            )
            SELECT e.key, e.name, weaker.seed_count, weaker.areas,
                   weaker.support AS balance,
                   stronger.area AS stronger_area, weaker.area AS weaker_area
            FROM ranked weaker
            JOIN ranked stronger ON stronger.key = weaker.key AND stronger.position = 1
            JOIN artist e ON e.key = weaker.key
            WHERE weaker.position = 2 AND weaker.areas >= :min_areas
            ORDER BY balance DESC, weaker.seed_count DESC, e.key
            LIMIT :limit
            """,
            {"min_areas": max(2, min_areas), "min_area_seeds": min_area_seeds, "limit": limit},
        )
    )
    for row in rows:
        row["seed_names"] = _area_seed_names(
            connection, row["key"], row.pop("stronger_area")
        ) + _area_seed_names(connection, row["key"], row.pop("weaker_area"))
    return rows


def _area_seed_names(
    connection: sqlite3.Connection, artist_key: str, area: int, count: int = 2
) -> list[str]:
    """The seeds in one area that name the artist, closest match first."""
    return [
        row[0]
        for row in connection.execute(
            """
            SELECT s.name FROM similar r JOIN artist s ON s.key = r.seed_key
            WHERE r.artist_key = ? AND s.area = ?
            ORDER BY r.score DESC, s.name
            LIMIT ?
            """,
            (artist_key, area, count),
        )
    ]


def library_titles(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT DISTINCT title FROM track")]


def stats(connection: sqlite3.Connection) -> dict:
    summary = dict(
        connection.execute(
            """
            SELECT count(*) AS artists,
                   coalesce(sum(degree = 0), 0) AS isolated_artists,
                   coalesce(sum(degree > 0), 0) AS connected_artists,
                   count(DISTINCT CASE WHEN component >= 0 THEN component END) AS components,
                   coalesce(max(component_size), 0) AS largest_component,
                   (SELECT count(*) FROM track) AS tracks,
                   (SELECT count(*) FROM container) AS containers
            FROM artist
            """
        ).fetchone()
    )
    summary["relationships"] = sum(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in EDGE_TABLES
    )
    return summary


def _dicts(rows: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in rows]

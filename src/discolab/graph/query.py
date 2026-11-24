from __future__ import annotations

import itertools
import json
import sqlite3

from .store import TABLES, keys_json

EDGE_TABLES = tuple(table for table in TABLES if table not in ("artist", "track", "container"))
# One number for both: the app looks up every area a discovery names.
MIN_AREA_SIZE = 5


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


def areas(connection: sqlite3.Connection, min_size: int = MIN_AREA_SIZE) -> list[dict]:
    """The taste areas, largest first, each with its artists most played first."""
    found: dict[int, dict] = {}
    for row in connection.execute(
        """
        SELECT a.area, a.name
        FROM artist a LEFT JOIN track t ON t.artist_key = a.key
        WHERE a.area >= 0
        GROUP BY a.key
        ORDER BY coalesce(sum(t.plays), 0) DESC, a.degree DESC, a.name
        """
    ):
        entry = found.setdefault(
            row["area"], {"area": row["area"], "artists": [], "styles": []}
        )
        entry["artists"].append(row["name"])

    for row in connection.execute(
        """
        SELECT a.area, style.value AS style, count(*) AS n
        FROM artist a, json_each(a.styles) AS style
        WHERE a.area >= 0
        GROUP BY a.area, style.value
        ORDER BY n DESC, style.value
        """
    ):
        found[row["area"]]["styles"].append((row["style"], row["n"]))
    kept = [entry for entry in found.values() if len(entry["artists"]) >= min_size]
    return sorted(kept, key=lambda entry: (-len(entry["artists"]), entry["area"]))


def track_paths(connection: sqlite3.Connection) -> list[dict]:
    return _dicts(
        connection.execute(
            "SELECT t.key, path.value AS path FROM track t, json_each(t.paths) AS path"
        )
    )


def external_candidates(
    connection: sqlite3.Connection,
    limit: int = 25,
    min_areas: int = 2,
    min_area_seeds: int = MIN_AREA_SIZE,
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
            SELECT e.key, e.name, e.url, e.styles, vote.seed_count, vote.areas,
                   weaker.support AS balance, vote.area, vote.support
            FROM ranked vote
            JOIN ranked weaker ON weaker.key = vote.key AND weaker.position = 2
            JOIN artist e ON e.key = vote.key
            WHERE vote.areas >= :min_areas
            ORDER BY balance DESC, vote.seed_count DESC, e.key, vote.position
            """,
            {"min_areas": max(2, min_areas), "min_area_seeds": min_area_seeds},
        )
    )
    by_artist = itertools.groupby(rows, key=lambda row: row["key"])
    wanted = itertools.islice(by_artist, limit if limit >= 0 else None)
    return [_candidate(connection, list(votes)) for _, votes in wanted]


def _candidate(connection: sqlite3.Connection, votes: list[dict]) -> dict:
    """One artist with every area that votes for it, strongest first."""
    artist = votes[0]
    return {
        "key": artist["key"],
        "name": artist["name"],
        "url": artist["url"],
        "styles": json.loads(artist["styles"]),
        "seed_count": artist["seed_count"],
        "areas": artist["areas"],
        "balance": artist["balance"],
        "votes": [
            {
                "area": vote["area"],
                "support": vote["support"],
                "seeds": _area_seed_names(connection, artist["key"], vote["area"]),
            }
            for vote in votes
        ],
    }


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

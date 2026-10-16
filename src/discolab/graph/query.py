from __future__ import annotations

from neo4j import Driver


def bridge_candidates(
    driver: Driver,
    database: str,
    limit: int = 20,
    min_degree: int = 2,
    min_bridge: float = 0.0,
) -> list[dict]:
    """Artists that sit on paths between otherwise separate parts of the graph."""
    records = driver.execute_query(
        """
        MATCH (a:Artist)
        WHERE a.degree >= $min_degree AND a.bridge_score > $min_bridge
        RETURN a.key AS key, a.name AS name, a.community AS community,
               a.component AS component, a.component_size AS component_size,
               a.bridge_score AS bridge_score, a.degree AS degree
        ORDER BY bridge_score DESC, degree DESC
        LIMIT $limit
        """,
        min_degree=min_degree,
        min_bridge=min_bridge,
        limit=limit,
        database_=database,
    ).records
    return [dict(record) for record in records]


def owned_tracks_for(
    driver: Driver, database: str, artist_keys: list[str], per_artist: int = 5
) -> dict[str, list[dict]]:
    records = driver.execute_query(
        """
        UNWIND $keys AS key
        MATCH (t:Track)-[:BY]->(a:Artist {key: key})
        WITH a.key AS artist_key, t ORDER BY coalesce(t.plays, 0) DESC, t.title
        WITH artist_key,
             collect({title: t.title, path: coalesce(t.paths[0], ''), plays: coalesce(t.plays, 0)})
               [0..$per_artist] AS tracks
        RETURN artist_key, tracks
        """,
        keys=artist_keys,
        per_artist=per_artist,
        database_=database,
    ).records
    return {record["artist_key"]: record["tracks"] for record in records}


def islands(driver: Driver, database: str, limit: int = 8, sample: int = 6) -> list[dict]:
    """Connected components of the artist graph, largest first.

    A component is a group of artists reachable from one another. Separate
    components have no edge between them, so nothing in the data relates them.
    """
    records = driver.execute_query(
        """
        MATCH (a:Artist)
        WHERE a.component >= 0
        WITH a.component AS component, a.component_size AS size, a
        ORDER BY a.degree DESC, a.name
        WITH component, size, collect(a.name)[0..$sample] AS members
        RETURN component, size, members
        ORDER BY size DESC
        LIMIT $limit
        """,
        sample=sample,
        limit=limit,
        database_=database,
    ).records
    return [dict(record) for record in records]


def library_titles(driver: Driver, database: str) -> list[str]:
    records = driver.execute_query(
        "MATCH (t:Track) RETURN DISTINCT t.title AS title",
        database_=database,
    ).records
    return [record["title"] for record in records]


def stats(driver: Driver, database: str) -> dict:
    record = driver.execute_query(
        """
        MATCH (a:Artist)
        WITH count(a) AS artists,
             sum(CASE WHEN coalesce(a.degree, 0) = 0 THEN 1 ELSE 0 END) AS isolated_artists
        MATCH (t:Track) WITH artists, isolated_artists, count(t) AS tracks
        MATCH (c:Container) WITH artists, isolated_artists, tracks, count(c) AS containers
        MATCH ()-[r]->() WITH artists, isolated_artists, tracks, containers, count(r) AS relationships
        MATCH (x:Artist) WHERE x.component >= 0
        WITH artists, isolated_artists, tracks, containers, relationships,
             count(DISTINCT x.component) AS components, max(x.component_size) AS largest_component
        MATCH (y:Artist) WHERE coalesce(y.degree, 0) > 0
        RETURN artists, isolated_artists, tracks, containers, relationships,
               components, largest_component, count(y) AS connected_artists
        """,
        database_=database,
    ).records[0]
    return dict(record)

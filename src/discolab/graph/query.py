from __future__ import annotations

from collections.abc import Sequence

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


def corpus_keys(driver: Driver, database: str) -> list[str]:
    """Keys of artists from the user's own data.

    Expansion output is excluded, or the second run would treat its own
    suggestions as things the user already has.
    """
    records = driver.execute_query(
        "MATCH (a:Artist) WHERE coalesce(a.external, false) = false RETURN a.key AS key",
        database_=database,
    ).records
    return [record["key"] for record in records]


def seed_artists(
    driver: Driver, database: str, per_community: int = 3, min_degree: int = 2
) -> list[dict]:
    """The most central members of each taste cluster, to expand from.

    Taking the top bridges alone concentrates the seeds in whichever island is
    largest. One artist per cluster keeps them spread across the taste, which
    is what makes two unrelated areas agreeing on a name mean something.
    """
    records = driver.execute_query(
        """
        MATCH (a:Artist)
        WHERE coalesce(a.external, false) = false
          AND a.community >= 0 AND coalesce(a.degree, 0) >= $min_degree
        WITH a.community AS community, a
        ORDER BY a.degree DESC, a.name
        WITH community, collect(a)[0..$per_community] AS members
        UNWIND members AS m
        RETURN m.key AS key, m.name AS name, m.community AS community, m.degree AS degree
        ORDER BY community, degree DESC
        """,
        per_community=per_community,
        min_degree=min_degree,
        database_=database,
    ).records
    return [dict(record) for record in records]


def artist_keys(driver: Driver, database: str) -> list[str]:
    records = driver.execute_query(
        "MATCH (a:Artist) RETURN a.key AS key", database_=database
    ).records
    return [record["key"] for record in records]


def external_candidates(
    driver: Driver,
    database: str,
    limit: int = 25,
    min_areas: int = 0,
    tags: Sequence[str] = (),
) -> list[dict]:
    """Artists from outside the corpus, ranked by how many seeds agree on them.

    One seed suggesting an artist says almost nothing, which is the bulk of a
    similarity list. Several seeds agreeing says more, and seeds sitting in
    different communities of the graph agreeing says most.

    When tags are given, only seeds carrying one of them count towards the
    agreement. Filtering the seeds at expansion time is not enough: the graph
    keeps every earlier run, so a non-electronic seed expanded months ago would
    still outvote the ones being asked for.
    """
    records = driver.execute_query(
        """
        MATCH (s:Artist)-[r:SIMILAR_TO]->(e:Artist)
        WHERE coalesce(e.external, false) = true
          AND ($tags = [] OR any(t IN coalesce(s.tags, []) WHERE t IN $tags))
        WITH e,
             count(DISTINCT s) AS seed_count,
             count(DISTINCT s.community) AS areas,
             avg(r.score) AS mean_score,
             max(r.score) AS best_score,
             collect(DISTINCT s.name)[0..4] AS seed_names
        WHERE areas >= $min_areas
        RETURN e.key AS key, e.name AS name, seed_count, areas,
               mean_score, best_score, seed_names
        ORDER BY seed_count DESC, areas DESC, mean_score DESC
        LIMIT $limit
        """,
        tags=list(tags),
        min_areas=min_areas,
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

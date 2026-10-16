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
    driver: Driver,
    database: str,
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
    records = driver.execute_query(
        """
        MATCH (a:Artist)
        WHERE coalesce(a.external, false) = false
          AND a.community >= 0 AND coalesce(a.degree, 0) >= $min_degree
        WITH a.community AS community, a
        ORDER BY a.degree DESC, a.name
        WITH community, collect(a)[0..$per_community] AS members
        UNWIND members AS m
        RETURN m.key AS key, m.name AS name, m.community AS community,
               m.degree AS degree, false AS isolated
        ORDER BY community, degree DESC
        """,
        per_community=per_community,
        min_degree=min_degree,
        database_=database,
    ).records
    seeds = [dict(record) for record in records]

    if isolated_limit > 0:
        lonely = driver.execute_query(
            """
            MATCH (a:Artist)
            WHERE coalesce(a.external, false) = false AND coalesce(a.degree, 0) = 0
            MATCH (t:Track)-[:BY]->(a)
            WITH a, count(t) AS tracks,
                 sum(CASE WHEN size(coalesce(t.paths, [])) > 0 THEN 1 ELSE 0 END) AS owned,
                 sum(coalesce(t.plays, 0)) AS plays
            WHERE owned > 0 OR plays > 0
            RETURN a.key AS key, a.name AS name, -1 AS community,
                   0 AS degree, true AS isolated
            ORDER BY tracks DESC, plays DESC
            LIMIT $isolated_limit
            """,
            isolated_limit=isolated_limit,
            database_=database,
        ).records
        seeds.extend(dict(record) for record in lonely)
    return seeds


def artist_sources(
    driver: Driver, database: str, keys: Sequence[str]
) -> dict[str, dict]:
    """Styles and labels per artist key, for whatever has been fetched so far."""
    records = driver.execute_query(
        """
        UNWIND $keys AS key
        MATCH (a:Artist {key: key})
        RETURN a.key AS key,
               coalesce(a.styles, []) AS styles,
               coalesce(a.labels, []) AS labels
        """,
        keys=list(keys),
        database_=database,
    ).records
    return {record["key"]: dict(record) for record in records}


def clusters(
    driver: Driver, database: str, limit: int = 40, sample: int = 6
) -> list[dict]:
    """The taste clusters, largest first, with their members and usual styles."""
    sizes = {
        record["cluster"]: dict(record)
        for record in driver.execute_query(
            """
            MATCH (a:Artist)
            WHERE a.community >= 0 AND coalesce(a.external, false) = false
            WITH a.community AS cluster, a
            ORDER BY a.degree DESC, a.name
            WITH cluster, collect(a.name)[0..$sample] AS members, count(*) AS size
            RETURN cluster, size, members
            ORDER BY size DESC
            LIMIT $limit
            """,
            sample=sample,
            limit=limit,
            database_=database,
        ).records
    }
    if not sizes:
        return []

    styles: dict[int, list[str]] = {}
    for record in driver.execute_query(
        """
        MATCH (a:Artist) WHERE a.community >= 0
        UNWIND coalesce(a.styles, []) AS style
        WITH a.community AS cluster, style, count(*) AS n
        ORDER BY n DESC
        WITH cluster, collect(style)[0..5] AS top_styles
        RETURN cluster, top_styles
        """,
        database_=database,
    ).records:
        styles[record["cluster"]] = record["top_styles"]

    for cluster, entry in sizes.items():
        entry["styles"] = styles.get(cluster, [])
    return sorted(sizes.values(), key=lambda entry: -entry["size"])


def cluster_frontier(
    driver: Driver, database: str, cluster: int, limit: int = 25
) -> list[dict]:
    """Artists outside the corpus reached from one cluster's seeds.

    Scoping to a cluster answers a different question from the global ranking:
    not what the whole taste agrees on, but what this corner of it leads to.
    """
    records = driver.execute_query(
        """
        MATCH (s:Artist)-[r:SIMILAR_TO]->(e:Artist)
        WHERE s.community = $cluster AND coalesce(e.external, false) = true
        WITH e, count(DISTINCT s) AS seed_count, max(r.score) AS best_score,
             collect(DISTINCT s.name)[0..4] AS seed_names
        RETURN e.key AS key, e.name AS name, seed_count, best_score, seed_names
        ORDER BY best_score DESC, seed_count DESC
        LIMIT $limit
        """,
        cluster=cluster,
        limit=limit,
        database_=database,
    ).records
    return [dict(record) for record in records]


def cluster_periphery(
    driver: Driver, database: str, cluster: int, limit: int = 25
) -> list[dict]:
    """The cluster's own artists that barely connect to it.

    A member with one edge sits at the edge of the taste rather than in it,
    which is where something recognisable but rarely played tends to live.
    """
    records = driver.execute_query(
        """
        MATCH (a:Artist)
        WHERE a.community = $cluster AND coalesce(a.external, false) = false
        RETURN a.key AS key, a.name AS name, a.community AS community,
               coalesce(a.degree, 0) AS degree, coalesce(a.bridge_score, 0.0) AS bridge_score
        ORDER BY degree ASC, a.name
        LIMIT $limit
        """,
        cluster=cluster,
        limit=limit,
        database_=database,
    ).records
    return [dict(record) for record in records]


def artists_needing_genres(
    driver: Driver,
    database: str,
    limit: int,
    external_only: bool = False,
    min_agreement: int = 2,
) -> list[dict]:
    """Artists Discogs has not been asked about, the ones that can surface first.

    Defaults to artists two or more seeds agree on, because those are the only
    ones that can reach a page. The 2,410 single-seed tail is forty minutes at
    sixty requests a minute and cannot change any output.

    `min_agreement=0` walks everything, for the rare case where that is wanted.
    """
    clause = "AND coalesce(a.external, false) = true" if external_only else ""
    records = driver.execute_query(
        f"""
        MATCH (a:Artist)
        WHERE coalesce(a.enriched, false) = false {clause}
        WITH a, size([(seed:Artist)-[:SIMILAR_TO]->(a) | seed]) AS agreement
        WHERE agreement >= $min_agreement
        ORDER BY agreement DESC, a.name
        LIMIT $limit
        RETURN a.key AS key, a.name AS name, agreement
        """,
        min_agreement=min_agreement,
        limit=limit,
        database_=database,
    ).records
    return [dict(record) for record in records]


def orphan_counts(driver: Driver, database: str) -> dict[str, int]:
    """Artists and labels with no relationships at all."""
    record = driver.execute_query(
        """
        MATCH (a:Artist) WHERE NOT (a)--()
        WITH count(a) AS artists
        MATCH (l:Label) WHERE NOT (l)--()
        RETURN artists, count(l) AS labels
        """,
        database_=database,
    ).records[0]
    return {"artists": record["artists"], "labels": record["labels"]}


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
) -> list[dict]:
    """Artists from outside the corpus, ranked by how many seeds agree on them.

    One seed suggesting an artist says almost nothing, which is the bulk of a
    similarity list. Several seeds agreeing says more, and seeds sitting in
    different communities of the graph agreeing says most.

    There is no genre predicate. Narrowing happens by scoping to a cluster, so
    the graph stays complete and the filter is a choice about where to look
    rather than a value baked into the rank.
    """
    records = driver.execute_query(
        """
        MATCH (s:Artist)-[r:SIMILAR_TO]->(e:Artist)
        WHERE coalesce(e.external, false) = true
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

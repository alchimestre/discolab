from __future__ import annotations

import networkx as nx
from neo4j import Driver

DEFAULT_RESOLUTION = 1.0
RANDOM_SEED = 42


def build_collaboration_edges(driver: Driver, database: str) -> int:
    """Connect two artists credited on the same track, excluding primary-to-primary."""
    result = driver.execute_query(
        """
        MATCH (a:Artist)-[:CREDITED]->(t:Track)-[:BY]->(b:Artist)
        WHERE a <> b
        WITH a, b, count(DISTINCT t) AS tracks
        MERGE (a)-[r:COLLABORATES]-(b)
        SET r.weight = tracks, r.via = 'credit'
        RETURN count(*) AS edges
        """,
        database_=database,
    )
    return result.records[0]["edges"]


def build_container_edges(
    driver: Driver, database: str, min_artists: int = 2, max_artists: int = 80
) -> int:
    """Connect artists sharing a container, skipping containers that are too broad to inform."""
    result = driver.execute_query(
        """
        MATCH (c:Container)-[:CONTAINS]->(:Track)-[:BY]->(a:Artist)
        WITH c, collect(DISTINCT a) AS artists
        WHERE size(artists) >= $min_artists AND size(artists) <= $max_artists
        UNWIND artists AS x
        UNWIND artists AS y
        WITH x, y WHERE x.key < y.key
        MERGE (x)-[r:CO_OCCURS]-(y)
        SET r.weight = coalesce(r.weight, 0) + 1, r.via = 'container'
        RETURN count(*) AS edges
        """,
        min_artists=min_artists,
        max_artists=max_artists,
        database_=database,
    )
    return result.records[0]["edges"]


def reconnect_isolated(driver: Driver, database: str) -> int:
    """Give artists with no cluster the cluster most of their similar artists share.

    A file-only artist has no co-occurrence with anyone and no credit naming
    another artist, so it sits outside every cluster even when it plainly
    belongs to one. Similarity edges carry the missing group membership, which
    is how a metal record buried in the filesystem finds the rest of the metal.
    """
    result = driver.execute_query(
        """
        MATCH (a:Artist) WHERE a.community < 0
        MATCH (a)-[r:SIMILAR_TO]-(n:Artist) WHERE n.community >= 0
        WITH a, n.community AS community, count(*) AS votes, sum(r.score) AS weight
        ORDER BY votes DESC, weight DESC
        WITH a, collect(community)[0] AS community
        SET a.community = community, a.reconnected = true
        RETURN count(*) AS reconnected
        """,
        database_=database,
    )
    return result.records[0]["reconnected"]


def assign_communities(
    driver: Driver, database: str, resolution: float = DEFAULT_RESOLUTION
) -> dict:
    """Louvain communities, connected components, and betweenness, written back as properties."""
    driver.execute_query(
        "MATCH (a:Artist) SET a.community = -1, a.component = -1, a.component_size = 0, "
        "a.bridge_score = 0.0, a.degree = 0",
        database_=database,
    )

    records = driver.execute_query(
        """
        MATCH (a:Artist)-[r:CO_OCCURS|COLLABORATES]-(b:Artist)
        WHERE a.key < b.key
        RETURN a.key AS source, b.key AS target, coalesce(r.weight, 1) AS weight
        """,
        database_=database,
    ).records

    graph = nx.Graph()
    for record in records:
        graph.add_edge(record["source"], record["target"], weight=record["weight"])

    if graph.number_of_nodes() == 0:
        return {"artists_in_graph": 0, "edges": 0, "communities": 0, "components": 0}

    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=resolution, seed=RANDOM_SEED
    )
    membership: dict[str, int] = {}
    for index, members in enumerate(sorted(communities, key=len, reverse=True)):
        for key in members:
            membership[key] = index

    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    component_of: dict[str, tuple[int, int]] = {}
    for index, members in enumerate(components):
        for key in members:
            component_of[key] = (index, len(members))

    betweenness = nx.betweenness_centrality(graph, normalized=True)

    rows = [
        {
            "key": key,
            "community": membership[key],
            "component": component_of[key][0],
            "component_size": component_of[key][1],
            "bridge_score": betweenness.get(key, 0.0),
            "degree": graph.degree(key),
        }
        for key in graph.nodes
    ]
    driver.execute_query(
        """
        UNWIND $rows AS row
        MATCH (a:Artist {key: row.key})
        SET a.community = row.community,
            a.component = row.component,
            a.component_size = row.component_size,
            a.bridge_score = row.bridge_score,
            a.degree = row.degree
        """,
        rows=rows,
        database_=database,
    )
    return {
        "artists_in_graph": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "communities": len(communities),
        "components": len(components),
        "largest_component": len(components[0]),
        "bridging_artists": sum(1 for value in betweenness.values() if value > 0),
    }

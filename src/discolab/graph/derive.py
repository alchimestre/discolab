from __future__ import annotations

import sqlite3

import networkx as nx

DEFAULT_RESOLUTION = 1.0
RANDOM_SEED = 42
# Coarser than the cluster resolution: at 1.0 one genre splits into several
# areas, and its own fragments then "agree" as if they were distinct tastes.
AREA_RESOLUTION = 0.3

# One weighted row per unordered artist pair. Ordered, because Louvain's result
# depends on the order edges are added in, even with a fixed seed.
ARTIST_EDGES = """
    SELECT source, target, sum(weight) AS weight FROM (
        SELECT min(seed_key, artist_key) AS source, max(seed_key, artist_key) AS target,
               coalesce(score, 1) AS weight
        FROM similar WHERE seed_key <> artist_key
        UNION ALL
        SELECT a, b, 2.0 FROM collaborates
    )
    {where}
    GROUP BY source, target
    ORDER BY source, target
"""
CORPUS_ONLY = """
    WHERE source IN (SELECT key FROM artist WHERE external = 0)
      AND target IN (SELECT key FROM artist WHERE external = 0)
"""


def build_collaboration_edges(connection: sqlite3.Connection) -> int:
    """Connect two artists credited on the same track, excluding primary-to-primary."""
    connection.execute("DELETE FROM collaborates")
    return connection.execute(
        """
        INSERT INTO collaborates (a, b, weight)
        SELECT min(c.artist_key, t.artist_key), max(c.artist_key, t.artist_key),
               count(DISTINCT t.key)
        FROM credited c JOIN track t ON t.key = c.track_key
        WHERE c.artist_key <> t.artist_key
        GROUP BY 1, 2
        """
    ).rowcount


def build_container_edges(
    connection: sqlite3.Connection, min_artists: int = 2, max_artists: int = 80
) -> int:
    """Connect artists sharing a container, skipping containers that are too broad to inform."""
    connection.execute("DELETE FROM co_occurs")
    connection.execute(
        """
        WITH member AS (
            SELECT DISTINCT c.container_key, t.artist_key
            FROM contains c JOIN track t ON t.key = c.track_key
        ),
        informative AS (
            SELECT container_key FROM member
            GROUP BY container_key HAVING count(*) BETWEEN :min_artists AND :max_artists
        )
        INSERT INTO co_occurs (a, b, weight)
        SELECT x.artist_key, y.artist_key, count(*)
        FROM informative
        JOIN member x USING (container_key)
        JOIN member y USING (container_key)
        WHERE x.artist_key < y.artist_key
        GROUP BY x.artist_key, y.artist_key
        """,
        {"min_artists": min_artists, "max_artists": max_artists},
    )
    # A statement opening with WITH reports a rowcount of -1.
    return connection.execute("SELECT count(*) FROM co_occurs").fetchone()[0]


def assign_communities(
    connection: sqlite3.Connection, resolution: float = DEFAULT_RESOLUTION
) -> dict:
    """Louvain communities, connected components, and betweenness, written back as columns."""
    connection.execute(
        "UPDATE artist SET community = -1, component = -1, component_size = 0, "
        "bridge_score = 0.0, degree = 0"
    )
    graph = _graph(connection, ARTIST_EDGES.format(where=""))
    if graph.number_of_nodes() == 0:
        return {
            "artists_in_graph": 0,
            "edges": 0,
            "communities": 0,
            "components": 0,
            "largest_component": 0,
            "bridging_artists": 0,
        }

    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=resolution, seed=RANDOM_SEED
    )
    membership = _largest_first(communities)
    components = sorted(
        nx.connected_components(graph), key=lambda members: (-len(members), min(members))
    )
    component_of = _largest_first(components)

    # Sampled: exact betweenness is O(nm), which on fifteen thousand artists and
    # sixty thousand edges is hours. Five hundred sources is plenty to rank them.
    betweenness = nx.betweenness_centrality(
        graph, k=min(500, graph.number_of_nodes()), normalized=True, seed=RANDOM_SEED
    )

    connection.executemany(
        "UPDATE artist SET community = ?, component = ?, component_size = ?, "
        "bridge_score = ?, degree = ? WHERE key = ?",
        [
            (
                membership[key],
                component_of[key],
                len(components[component_of[key]]),
                betweenness.get(key, 0.0),
                graph.degree(key),
                key,
            )
            for key in graph.nodes
        ],
    )
    return {
        "artists_in_graph": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "communities": len(communities),
        "components": len(components),
        "largest_component": len(components[0]),
        "bridging_artists": sum(1 for value in betweenness.values() if value > 0),
    }


def assign_areas(connection: sqlite3.Connection, resolution: float = AREA_RESOLUTION) -> int:
    """Louvain over the user's own artists only, written as `artist.area`.

    Communities include expansion artists, and one that links two areas pulls
    them into a single community, which erases the intersection being ranked.
    """
    connection.execute("UPDATE artist SET area = -1")
    graph = _graph(connection, ARTIST_EDGES.format(where=CORPUS_ONLY))
    if graph.number_of_nodes() == 0:
        return 0
    areas = nx.community.louvain_communities(
        graph, weight="weight", resolution=resolution, seed=RANDOM_SEED
    )
    connection.executemany(
        "UPDATE artist SET area = ? WHERE key = ?",
        [(area, key) for key, area in _largest_first(areas).items()],
    )
    return len(areas)


def _graph(connection: sqlite3.Connection, edges_sql: str) -> nx.Graph:
    graph = nx.Graph()
    graph.add_weighted_edges_from(connection.execute(edges_sql))
    return graph


def _largest_first(groups: list[set[str]]) -> dict[str, int]:
    """Each member's group index, numbering groups from the largest down."""
    ordered = sorted(groups, key=lambda members: (-len(members), min(members)))
    return {key: index for index, members in enumerate(ordered) for key in members}

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from neo4j import Driver

from ..model import Catalog

BATCH_SIZE = 2000


def load_catalog(driver: Driver, database: str, catalog: Catalog) -> None:
    _write_nodes(
        driver,
        database,
        "Artist",
        [{"key": artist.key, "name": artist.name} for artist in catalog.artists],
    )
    _write_nodes(
        driver,
        database,
        "Container",
        [
            {"key": container.key, "kind": container.kind, "name": container.name}
            for container in catalog.containers
        ],
    )
    _write_tracks(driver, database, catalog)
    _write_relations(
        driver,
        database,
        "UNWIND $rows AS row "
        "MATCH (c:Container {key: row.left}) MATCH (t:Track {key: row.right}) "
        "MERGE (c)-[:CONTAINS]->(t)",
        [(container_key, track_key) for container_key, track_key in catalog.memberships],
    )
    _write_relations(
        driver,
        database,
        "UNWIND $rows AS row "
        "MATCH (a:Artist {key: row.left}) MATCH (t:Track {key: row.right}) "
        "MERGE (a)-[:CREDITED {role: row.role}]->(t)",
        [
            (credit.artist_key, credit.track_key, credit.role)
            for credit in catalog.credits
        ],
    )


def _write_nodes(
    driver: Driver, database: str, label: str, rows: Sequence[dict[str, Any]]
) -> None:
    for chunk in _chunks(rows, BATCH_SIZE):
        driver.execute_query(
            f"UNWIND $rows AS row MERGE (n:{label} {{key: row.key}}) SET n += row",
            rows=chunk,
            database_=database,
        )


def _write_tracks(driver: Driver, database: str, catalog: Catalog) -> None:
    rows = [
        {
            "key": track.key,
            "title": track.title,
            "artist_key": track.artist_key,
            "duration_ms": track.duration_ms,
            "paths": list(track.paths),
            "plays": track.plays,
        }
        for track in catalog.tracks
    ]
    for chunk in _chunks(rows, BATCH_SIZE):
        driver.execute_query(
            """
            UNWIND $rows AS row
            MERGE (t:Track {key: row.key})
            SET t.title = row.title, t.duration_ms = row.duration_ms,
                t.paths = row.paths, t.plays = row.plays
            WITH t, row
            MATCH (a:Artist {key: row.artist_key})
            MERGE (t)-[:BY]->(a)
            """,
            rows=chunk,
            database_=database,
        )


def _write_relations(
    driver: Driver, database: str, statement: str, rows: Iterable[tuple]
) -> None:
    materialised = list(rows)
    for chunk in _chunks(materialised, BATCH_SIZE):
        payload = [
            {"left": row[0], "right": row[1], "role": row[2] if len(row) > 2 else None}
            for row in chunk
        ]
        driver.execute_query(statement, rows=payload, database_=database)


def _chunks(rows: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]

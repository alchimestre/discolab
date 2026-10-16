from __future__ import annotations

from neo4j import Driver, GraphDatabase

from ..config import Settings

NODE_KEYS = {"Artist": "key", "Track": "key", "Container": "key"}


def connect(settings: Settings) -> Driver:
    return GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )


def ensure_schema(driver: Driver, database: str) -> None:
    for label, prop in NODE_KEYS.items():
        driver.execute_query(
            f"CREATE CONSTRAINT {label.lower()}_{prop} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE",
            database_=database,
        )


def delete_orphans(driver: Driver, database: str) -> dict[str, int]:
    """Remove artists and labels with no relationships, in batches."""
    removed = {}
    for key, node_label in (("artists", "Artist"), ("labels", "Label")):
        total = 0
        while True:
            result = driver.execute_query(
                f"""
                MATCH (n:{node_label}) WHERE NOT (n)--()
                WITH n LIMIT 5000
                DELETE n
                RETURN count(*) AS removed
                """,
                database_=database,
            )
            count = result.records[0]["removed"]
            total += count
            if count == 0:
                break
        removed[key] = total
    return removed


def clear_discogs_flag(driver: Driver, database: str, external_only: bool) -> int:
    """Forget that Discogs was consulted, so it is consulted again."""
    clause = "AND coalesce(a.external, false) = true" if external_only else ""
    result = driver.execute_query(
        f"""
        MATCH (a:Artist) WHERE coalesce(a.discogs_checked, false) = true {clause}
        SET a.discogs_checked = false
        RETURN count(a) AS cleared
        """,
        database_=database,
    )
    return result.records[0]["cleared"]


def clear(driver: Driver, database: str) -> None:
    driver.execute_query("MATCH (n) DETACH DELETE n", database_=database)

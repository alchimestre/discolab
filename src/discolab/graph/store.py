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


def clear(driver: Driver, database: str) -> None:
    driver.execute_query("MATCH (n) DETACH DELETE n", database_=database)

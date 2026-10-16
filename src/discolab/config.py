from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    library_roots: tuple[Path, ...]
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str

    @classmethod
    def from_env(cls, roots: tuple[Path, ...] | None = None) -> "Settings":
        return cls(
            library_roots=roots if roots is not None else _roots_from_env(),
            neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
            neo4j_password=os.environ.get("NEO4J_PASSWORD", "discolab-dev"),
            neo4j_database=os.environ.get("NEO4J_DATABASE", "neo4j"),
        )


def load_env_file(path: Path = Path(".env")) -> None:
    """Load KEY=value pairs from a dotenv file without overriding the environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _roots_from_env() -> tuple[Path, ...]:
    raw = os.environ.get("DISCOLAB_LIBRARY_ROOTS", "")
    parts = [part for part in raw.split(os.pathsep) if part.strip()]
    return tuple(Path(part).expanduser().resolve() for part in parts)

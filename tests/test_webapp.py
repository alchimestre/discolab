from __future__ import annotations

from discolab.config import Settings
from discolab.graph import derive, load, store
from discolab.model import Artist, Catalog, Track
from discolab.webapp import graph_payload

TECHNO = ("surgeon", "regis", "female", "ancient methods", "blawan")
DESERT = ("tinariwen", "tartit", "bombino", "terakaft", "tamikrest")


def _similar(seed: str, name: str, external: bool) -> dict:
    return {
        "key": name,
        "name": name.title(),
        "seed_key": seed,
        "score": 1.0,
        "url": None,
        "source": "test",
        "external": external,
    }


def test_every_area_a_discovery_names_is_in_the_payload(tmp_path) -> None:
    keys = TECHNO + DESERT
    catalog = Catalog(
        artists=tuple(Artist(key, key.title()) for key in keys),
        tracks=tuple(Track(f"{key}|one", "One", key, key.title(), None, ()) for key in keys),
        containers=(),
        memberships=(),
        credits=(),
        skipped=(),
    )
    rows = []
    for area in (TECHNO, DESERT):
        rows += [_similar(a, b, False) for a in area for b in area if a < b]
        rows += [_similar(seed, "between", True) for seed in area[:2]]

    settings = Settings(library_roots=(), database_path=tmp_path / "graph.db")
    with store.connect(settings) as connection:
        load.load_catalog(connection, catalog)
        load.write_expansion(connection, rows)
        derive.assign_communities(connection)
        derive.assign_areas(connection)
        payload = graph_payload(connection)

    area_ids = {area["id"] for area in payload["areas"]}
    assert len(area_ids) == 2
    assert [discovery["key"] for discovery in payload["discoveries"]] == ["between"]
    assert {vote["area"] for vote in payload["discoveries"][0]["votes"]} == area_ids
    assert payload["links"] == [{"a": min(area_ids), "b": max(area_ids), "shared": 1}]

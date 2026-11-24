from __future__ import annotations

from discolab.config import Settings
from discolab.graph import derive, load, query, store
from discolab.model import Artist, Catalog, Track

TECHNO = ("surgeon", "regis", "female")
DESERT = ("tinariwen", "tartit", "bombino")
DUB = ("scientist", "prince jammy", "king tubby")


def _catalog(keys: tuple[str, ...] = TECHNO + DESERT) -> Catalog:
    return Catalog(
        artists=tuple(Artist(key, key.title()) for key in keys),
        tracks=tuple(Track(f"{key}|one", "One", key, key.title(), None, ()) for key in keys),
        containers=(),
        memberships=(),
        credits=(),
        skipped=(),
    )


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


def test_only_an_artist_two_areas_vote_for_is_ranked(tmp_path) -> None:
    settings = Settings(library_roots=(), database_path=tmp_path / "graph.db")
    rows = []
    for area in (TECHNO, DESERT):
        rows += [_similar(a, b, False) for a in area for b in area if a < b]
        rows += [_similar(seed, "between", True) for seed in area[:2]]
    rows += [_similar(seed, "techno only", True) for seed in TECHNO]
    rows.append(_similar("surgeon", "named once", True))

    with store.connect(settings) as connection:
        load.load_catalog(connection, _catalog())
        load.write_expansion(connection, rows)
        assert load.drop_single_seed_artists(connection) == 1
        derive.assign_communities(connection)
        assert derive.assign_areas(connection) == 2
        ranked = query.external_candidates(connection, min_area_seeds=3)

    assert [row["key"] for row in ranked] == ["between"]
    assert ranked[0]["balance"] == 2 / 3
    assert ranked[0]["seed_count"] == 4


def test_a_candidate_carries_every_area_that_votes_for_it(tmp_path) -> None:
    settings = Settings(library_roots=(), database_path=tmp_path / "graph.db")
    rows = []
    for area, voters in ((TECHNO, 3), (DESERT, 2), (DUB, 2)):
        rows += [_similar(a, b, False) for a in area for b in area if a < b]
        rows += [_similar(seed, "everywhere", True) for seed in area[:voters]]

    with store.connect(settings) as connection:
        load.load_catalog(connection, _catalog(TECHNO + DESERT + DUB))
        load.write_expansion(connection, rows)
        derive.assign_areas(connection)
        ranked = query.external_candidates(connection, limit=-1, min_area_seeds=3)

    assert [row["key"] for row in ranked] == ["everywhere"]
    assert [vote["support"] for vote in ranked[0]["votes"]] == [1, 2 / 3, 2 / 3]
    assert ranked[0]["balance"] == 2 / 3

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from .check import (
    Candidate,
    Video,
    build_page,
    load_video_cache,
    read_votes,
    resolve_artist,
    save_video_cache,
    serve_directory,
    write_page,
)
from .config import Settings, load_env_file
from .enrich import (
    DEFAULT_DB,
    MUSICBRAINZ_INTERVAL,
    DiscogsEnricher,
    DiscogsLabelExpander,
    MusicBrainzEnricher,
    RekordboxEnricher,
    is_real_label,
    join_key,
)
from .expand import LastfmSimilarExpander
from .graph import derive, load, query, store
from .model import RawTrack
from .providers import (
    LastfmError,
    LastfmProvider,
    LocalFilesProvider,
    SpotifyClient,
    SpotifyError,
    SpotifyProvider,
)
from .resolve import build_catalog, normalise

app = typer.Typer(add_completion=False, help="Taste graph over local music metadata.")


@app.command()
def ingest(
    root: list[Path] = typer.Option(None, "--root", help="Library root, repeatable."),
    recreate: bool = typer.Option(
        False, "--recreate", help="Delete existing graph data before loading."
    ),
    lastfm: bool = typer.Option(
        False, "--lastfm", help="Also read scrobbles, which add play counts and listening sessions."
    ),
    spotify: bool = typer.Option(
        False, "--spotify", help="Also read Spotify playlists and liked songs."
    ),
    scrobbles: int = typer.Option(
        20000, "--scrobbles", help="How much scrobble history to read."
    ),
    months: int = typer.Option(
        0,
        "--months",
        help="Only read the last N months of scrobbles, 0 for all history. Tastes move.",
    ),
) -> None:
    """Read the configured providers and load observed nodes and edges into Neo4j."""
    settings = _settings(root)
    raw: list[RawTrack] = []
    unparsed: list[tuple[str, str]] = []

    if settings.library_roots:
        scan = LocalFilesProvider(settings.library_roots).scan()
        raw.extend(scan.tracks)
        unparsed.extend(scan.skipped)
        typer.echo(
            f"local files: {len(scan.tracks)} in {len(settings.library_roots)} root(s), "
            f"skipped {len(scan.skipped)}"
        )
    elif not lastfm:
        typer.echo("no library roots: pass --root or set DISCOLAB_LIBRARY_ROOTS", err=True)
        raise typer.Exit(code=2)

    if lastfm:
        if not settings.lastfm_api_key or not settings.lastfm_username:
            typer.echo("set LASTFM_API_KEY and LASTFM_USERNAME in .env", err=True)
            raise typer.Exit(code=2)
        typer.echo(f"last.fm: reading up to {scrobbles} scrobbles, this takes a few minutes...")
        since = None
        if months > 0:
            since = datetime.now(timezone.utc) - timedelta(days=int(months * 30.44))
            typer.echo(f"last.fm: only scrobbles since {since:%Y-%m-%d}")
        try:
            scrobble_scan = LastfmProvider(
                settings.lastfm_api_key, settings.lastfm_username, scrobbles, since=since
            ).scan()
        except LastfmError as error:
            typer.echo(f"last.fm failed: {error}", err=True)
            raise typer.Exit(code=3) from error
        raw.extend(scrobble_scan.tracks)
        unparsed.extend(scrobble_scan.skipped)
        typer.echo(f"last.fm: {len(scrobble_scan.tracks)} distinct tracks")

    if spotify:
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            typer.echo("set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env", err=True)
            raise typer.Exit(code=2)
        client = SpotifyClient(
            settings.spotify_client_id, settings.spotify_client_secret, TOKEN_PATH
        )
        if not client.has_login:
            typer.echo("not logged in: run `discolab spotify-login` first", err=True)
            raise typer.Exit(code=2)
        typer.echo("spotify: reading playlists and liked songs...")
        try:
            spotify_scan = SpotifyProvider(client).scan()
        except SpotifyError as error:
            typer.echo(f"spotify failed: {error}", err=True)
            raise typer.Exit(code=3) from error
        raw.extend(spotify_scan.tracks)
        unparsed.extend(spotify_scan.skipped)
        typer.echo(f"spotify: {len(spotify_scan.tracks)} distinct tracks")
        unreadable = [item for item in spotify_scan.skipped if "403" in item[1]]
        if unreadable:
            typer.echo(
                f"spotify: {len(unreadable)} playlists unreadable. A development-mode app "
                "cannot read the contents of a playlist it does not own."
            )

    catalog = build_catalog(raw)
    played = sum(1 for track in catalog.tracks if track.plays)
    typer.echo(
        f"resolved {len(catalog.artists)} artists, {len(catalog.tracks)} tracks "
        f"({played} played), {len(catalog.credits)} credits, "
        f"{len(catalog.containers)} containers, {len(catalog.memberships)} memberships"
    )
    if catalog.skipped:
        typer.echo(f"unresolved {len(catalog.skipped)}")
        for path, reason in catalog.skipped[:5]:
            typer.echo(f"  {reason}: {path}")

    with store.connect(settings) as driver:
        store.ensure_schema(driver, settings.neo4j_database)
        if recreate:
            store.clear(driver, settings.neo4j_database)
        load.load_catalog(driver, settings.neo4j_database, catalog)
    typer.echo("loaded")


@app.command("derive")
def derive_edges(
    resolution: float = typer.Option(1.0, "--resolution", help="Louvain resolution."),
    min_artists: int = typer.Option(2, "--min-artists", help="Smallest container that counts."),
    max_artists: int = typer.Option(
        80, "--max-artists", help="Largest container that counts; above this every track co-occurs."
    ),
) -> None:
    """Compute artist to artist edges, communities, and bridge scores."""
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        credits = derive.build_collaboration_edges(driver, database)
        containers = derive.build_container_edges(driver, database, min_artists, max_artists)
        summary = derive.assign_communities(driver, database, resolution)
        reconnected = derive.reconnect_isolated(driver, database)

    typer.echo(f"credit edges: {credits}, container edges: {containers}")
    typer.echo(
        f"graph: {summary['artists_in_graph']} artists, {summary['edges']} edges, "
        f"{summary['communities']} communities, {summary['components']} islands"
    )
    typer.echo(
        f"largest island: {summary['largest_component']} artists, "
        f"bridging artists: {summary['bridging_artists']}"
    )
    typer.echo(f"artists given a cluster from their similar artists: {reconnected}")


@app.command()
def candidates(
    limit: int = typer.Option(20, "--limit", help="How many artists to return."),
    min_degree: int = typer.Option(2, "--min-degree", help="Minimum artist degree."),
    min_bridge: float = typer.Option(
        0.0, "--min-bridge", help="Minimum betweenness; above zero means genuinely on a path."
    ),
    tracks: bool = typer.Option(True, "--tracks/--no-tracks", help="List owned tracks."),
) -> None:
    """Rank artists sitting between otherwise separate parts of the graph."""
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        rows = query.bridge_candidates(driver, database, limit, min_degree, min_bridge)
        if not rows:
            _explain_empty(driver, database)
            raise typer.Exit(code=0)
        owned = (
            query.owned_tracks_for(driver, database, [row["key"] for row in rows])
            if tracks
            else {}
        )

    for index, row in enumerate(rows, start=1):
        typer.echo(
            f"{index:2}. {row['name']}  community {row['community']}  "
            f"island {row['component']} ({row['component_size']} artists)  "
            f"degree {row['degree']}  bridge {row['bridge_score']:.5f}"
        )
        for track in owned.get(row["key"], []):
            typer.echo(f"      {track['title']}  {track['path']}")


@app.command()
def stats(island_count: int = typer.Option(8, "--islands", help="Islands to list.")) -> None:
    """Print node, edge, and island counts, then the largest islands."""
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        summary = query.stats(driver, database)
        island_rows = query.islands(driver, database, island_count)
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")
    typer.echo("islands (largest first):")
    for island in island_rows:
        typer.echo(f"  {island['size']:4}  {', '.join(island['members'])}")


@app.command()
def votes(
    path: Path = typer.Option(Path("out/votes.json"), "--path", help="Where the ticks are saved."),
) -> None:
    """Summarise the ticks from the listening page, and read the criterion."""
    saved = read_votes(path)
    if not saved:
        typer.echo(f"no ticks recorded at {path} yet")
        raise typer.Exit(code=0)

    kept = {name: vote for name, vote in saved.items() if vote.get("kept")}
    fresh = [name for name, vote in kept.items() if vote.get("state", "").startswith("new")]
    held = [
        name
        for name, vote in kept.items()
        if vote.get("state", "").startswith(("already", "played"))
    ]
    typer.echo(f"kept {len(kept)} of {len(saved)} candidates")
    typer.echo(f"  new tracks: {len(fresh)}")
    typer.echo(f"  already known: {len(held)}")
    typer.echo(
        "criterion met" if len(fresh) >= 3 else "criterion not met: fewer than 3 new tracks kept"
    )
    for name in sorted(kept):
        typer.echo(f"    {kept[name].get('state', '?'):14} {name}")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt.")) -> None:
    """Delete every node and relationship in the database."""
    settings = _settings([])
    if not yes:
        typer.confirm(f"delete all data in {settings.neo4j_uri}/{settings.neo4j_database}?", abort=True)
    with store.connect(settings) as driver:
        store.clear(driver, settings.neo4j_database)
    typer.echo("cleared")


@app.command()
def run(
    root: list[Path] = typer.Option(None, "--root", help="Library root, repeatable."),
    limit: int = typer.Option(20, "--limit"),
    min_degree: int = typer.Option(2, "--min-degree"),
    lastfm: bool = typer.Option(False, "--lastfm", help="Also read scrobbles."),
) -> None:
    """Ingest, derive, and print candidates in one pass."""
    ingest(root=root, recreate=True, lastfm=lastfm, scrobbles=20000)
    derive_edges(resolution=1.0, min_artists=2, max_artists=80)
    candidates(limit=limit, min_degree=min_degree, min_bridge=0.0, tracks=True)


@app.command()
def expand(
    per_cluster: int = typer.Option(
        3, "--per-cluster", help="Most central members to take from each taste cluster."
    ),
    per_seed: int = typer.Option(20, "--per-seed", help="Similar artists to ask for per seed."),
    min_degree: int = typer.Option(2, "--min-degree", help="Minimum seed degree."),
    isolated: int = typer.Option(
        0,
        "--isolated",
        help="Also seed from this many artists that have no co-occurrence at all, "
        "best represented first. Nothing else can reach them.",
    ),
    genre: str = typer.Option(
        "",
        "--genre",
        help="Only expand from seeds carrying one of these tags, comma separated. "
        "For example: electronic,techno,house.",
    ),
    min_score: float = typer.Option(
        0.0, "--min-score", help="Ignore matches weaker than this."
    ),
    by: str = typer.Option(
        "similar", "--by", help="similar expands through Last.fm; labels through Discogs."
    ),
    labels: int = typer.Option(
        40, "--labels", help="Labels to expand through when --by labels."
    ),
    min_label_artists: int = typer.Option(
        1, "--min-label-artists", help="Only labels already holding this many artists."
    ),
) -> None:
    """Expand beyond your own music, by similarity or through labels."""
    settings = _settings([])
    if by == "labels":
        _expand_through_labels(settings, labels, min_label_artists)
        return
    if not settings.lastfm_api_key:
        typer.echo("set LASTFM_API_KEY in .env", err=True)
        raise typer.Exit(code=2)

    with store.connect(settings) as driver:
        database = settings.neo4j_database
        seed_rows = query.seed_artists(driver, database, per_cluster, min_degree, isolated)
        known = set(query.corpus_keys(driver, database))
    if not seed_rows:
        typer.echo("no seeds: run `discolab derive` first", err=True)
        raise typer.Exit(code=2)

    expander = LastfmSimilarExpander(settings.lastfm_api_key, per_seed)
    wanted = {tag.strip().casefold() for tag in genre.split(",") if tag.strip()}

    typer.echo(f"tagging {len(seed_rows)} seeds")
    tag_rows = []
    for position, seed in enumerate(seed_rows, start=1):
        seed["tags"] = sorted({tag.casefold() for tag in expander.tags(seed["name"])})
        tag_rows.append({"key": seed["key"], "tags": seed["tags"]})
        if position % 25 == 0:
            typer.echo(f"  tagged {position}/{len(seed_rows)}", err=True)
    with store.connect(settings) as driver:
        load.write_seed_tags(driver, settings.neo4j_database, tag_rows)

    if wanted:
        seed_rows = [seed for seed in seed_rows if wanted & set(seed["tags"])]
        typer.echo(f"seeds tagged {', '.join(sorted(wanted))}: {len(seed_rows)}")
        if not seed_rows:
            typer.echo("no seed carried any of those tags", err=True)
            raise typer.Exit(code=2)
    typer.echo(f"expanding from {len(seed_rows)} seeds across taste clusters")

    found = 0
    linked = 0
    # One row per seed and artist, so that several seeds agreeing on the same
    # artist survives to the graph. Keying by artist alone lost the agreement.
    # A suggestion already in the corpus keeps its edge: that is a link between
    # two of the user's artists rather than a candidate.
    fresh: dict[tuple[str, str], dict] = {}
    for position, seed in enumerate(seed_rows, start=1):
        for suggestion in expander.similar(seed["name"]):
            found += 1
            key = normalise(suggestion.name)
            if not key:
                continue
            if suggestion.score < min_score:
                continue
            corpus = key in known
            if corpus:
                linked += 1
            pair = (key, seed["key"])
            current = fresh.get(pair)
            if current is None or suggestion.score > current["score"]:
                fresh[pair] = {
                    "key": key,
                    "name": suggestion.name,
                    "seed_key": seed["key"],
                    "seed": seed["name"],
                    "score": suggestion.score,
                    "source": expander.source,
                    "external": not corpus,
                }
        typer.echo(f"  expanded {position}/{len(seed_rows)}", err=True)

    with store.connect(settings) as driver:
        load.write_expansion(driver, settings.neo4j_database, list(fresh.values()))

    share = f"{linked / found:.0%}" if found else "n/a"
    fresh_count = sum(1 for row in fresh.values() if row["external"])
    typer.echo(f"suggestions from {len(seed_rows)} seeds: {found}")
    typer.echo(f"links between your own artists: {linked} ({share})")
    typer.echo(f"new artists written: {fresh_count}")


@app.command()
def enrich(
    rekordbox: Path = typer.Option(
        DEFAULT_DB, "--rekordbox", help="Path to the rekordbox master.db."
    ),
    skip_rekordbox: bool = typer.Option(
        False, "--skip-rekordbox", help="Leave the rekordbox step alone."
    ),
    musicbrainz: bool = typer.Option(
        False,
        "--musicbrainz",
        help="Identify artists against MusicBrainz. One request a second, and it resumes.",
    ),
    discogs: bool = typer.Option(
        False,
        "--discogs",
        help="Fetch styles and labels for artists. One request each, and it resumes.",
    ),
    external_only: bool = typer.Option(
        False, "--external-only", help="Limit the Discogs pass to artists from expansion."
    ),
    keep_going: bool = typer.Option(
        False, "--keep-going", help="Repeat until every artist has been asked about."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ask again about artists already done. Fixes stored noise."
    ),
    limit: int = typer.Option(
        200, "--limit", help="Artists to identify in this run; the next run continues."
    ),
) -> None:
    """Enrich tracks and artists from curated sources."""
    settings = _settings([])

    if not skip_rekordbox:
        _enrich_from_rekordbox(settings, rekordbox)

    if musicbrainz:
        _identify_with_musicbrainz(settings, limit)

    if discogs:
        if refresh:
            with store.connect(settings) as driver:
                store.clear_discogs_flag(driver, settings.neo4j_database, external_only)
        _enrich_artists_from_discogs(settings, limit, external_only, keep_going)


def _enrich_artists_from_discogs(
    settings: Settings, limit: int, external_only: bool, keep_going: bool = False
) -> None:
    """Walk the artists Discogs has not been asked about, writing as it goes.

    Nothing waits on this. It runs beside the page build, so a slow API costs
    throughput rather than blocking anything.
    """
    enricher = DiscogsEnricher(os.environ.get("DISCOGS_TOKEN", ""))
    reason = enricher.unavailable_reason()
    if reason:
        typer.echo(reason, err=True)
        raise typer.Exit(code=2)

    asked = 0
    answered = 0
    while True:
        with store.connect(settings) as driver:
            pending = query.artists_needing_discogs(
                driver, settings.neo4j_database, limit, external_only
            )
        if not pending:
            break

        typer.echo(f"asking Discogs about {len(pending)} artists")
        rows: list[dict] = []
        for position, artist in enumerate(pending, start=1):
            facts = enricher.facts(artist["name"])
            if facts is None:
                # Record the miss so the next pass does not ask again.
                rows.append({"key": artist["key"], "styles": [], "labels": []})
            else:
                answered += 1
                rows.append(
                    {
                        "key": artist["key"],
                        "styles": list(facts.styles),
                        "labels": list(facts.labels),
                    }
                )
            asked += 1
            if position % 50 == 0:
                typer.echo(f"  asked {position}/{len(pending)}", err=True)
                with store.connect(settings) as driver:
                    load.write_artist_sources(driver, settings.neo4j_database, rows)
                rows = []
        if rows:
            with store.connect(settings) as driver:
                load.write_artist_sources(driver, settings.neo4j_database, rows)
        if not keep_going:
            break

    typer.echo(f"Discogs asked about {asked}, answered for {answered}")


def _enrich_from_rekordbox(settings: Settings, rekordbox: Path) -> None:
    enricher = RekordboxEnricher(rekordbox)
    reason = enricher.unavailable_reason()
    if reason:
        typer.echo(reason, err=True)
        raise typer.Exit(code=2)

    typer.echo("reading rekordbox")
    facts = enricher.facts()
    typer.echo(
        f"  {len(facts)} rows, {sum(1 for f in facts.values() if f.bpm)} with a BPM, "
        f"{sum(1 for f in facts.values() if f.tags)} carrying tags"
    )

    with store.connect(settings) as driver:
        database = settings.neo4j_database
        pairs = query.track_paths(driver, database)
        rows: list[dict] = []
        seen: set[str] = set()
        for pair in pairs:
            sheet = facts.get(join_key(pair["path"]))
            if sheet is None or pair["key"] in seen:
                continue
            seen.add(pair["key"])
            rows.append(
                {
                    "key": pair["key"],
                    "bpm": sheet.bpm,
                    "musical_key": sheet.musical_key,
                    "rating": sheet.rating,
                    "dj_plays": sheet.dj_plays,
                    "tags": list(sheet.tags),
                }
            )
        load.write_track_facts(driver, database, rows)
    typer.echo(f"enriched {len(rows)} tracks from {len(pairs)} paths")


def _identify_with_musicbrainz(settings: Settings, limit: int) -> None:
    enricher = MusicBrainzEnricher()
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        pending = query.artists_to_identify(driver, database, limit)
    if not pending:
        typer.echo("every artist with tracks has been identified already")
    else:
        typer.echo(f"identifying {len(pending)} artists, one request a second")
        rows = []
        found = 0
        for position, artist in enumerate(pending, start=1):
            facts = enricher.lookup(artist["name"])
            time.sleep(MUSICBRAINZ_INTERVAL)
            if facts is None:
                rows.append(
                    {"key": artist["key"], "mbid": None, "mb_type": None,
                     "mb_country": None, "tags": []}
                )
                continue
            found += 1
            rows.append(
                {
                    "key": artist["key"],
                    "mbid": facts.mbid,
                    "mb_type": facts.kind,
                    "mb_country": facts.country,
                    "tags": list(facts.tags),
                }
            )
            if position % 25 == 0:
                typer.echo(f"  identified {position}/{len(pending)}", err=True)
        with store.connect(settings) as driver:
            load.write_artist_identity(driver, settings.neo4j_database, rows)
            duplicates = query.duplicate_identities(driver, settings.neo4j_database)
        typer.echo(f"identified {found} of {len(pending)} artists")

    if duplicates:
        total = sum(len(entry["nodes"]) for entry in duplicates)
        typer.echo(
            f"{len(duplicates)} identities span {total} artist nodes, which is "
            f"{total - len(duplicates)} duplicates to merge"
        )
        for entry in duplicates[:8]:
            names = ", ".join(node["name"] for node in entry["nodes"])
            typer.echo(f"  {names[:78]}")


# What "electronic" means when it is asked for by name, across Last.fm tags,
# Discogs styles and the rekordbox My Tags in use.
ELECTRONIC_TAGS = (
    "electronic,electronica,techno,house,psytrance,downtempo,idm,trance,ambient,"
    "minimal,electro,breakbeat,drum n bass,dub techno,acid,ebm,industrial,gabber,"
    "hardcore,rave,progressive trance,tech house,deep house,minimal techno,"
    "raw / deep / hypnotic,peak time / driving,nocturnal / afterhours,hard techno,"
    "minimal / deep tech,melodic house & techno,progressive psy-trance,detroit / deep"
)


def _genre_terms(genre: str) -> tuple[str, ...]:
    """Turn the genre string into terms, expanding the shorthand names."""
    text = genre.strip()
    if not text:
        return ()
    if text.casefold() == "electronic":
        text = ELECTRONIC_TAGS
    return tuple(tag.strip().casefold() for tag in text.split(",") if tag.strip())


def _expand_through_labels(settings: Settings, limit: int, min_artists: int) -> None:
    """Find other artists on labels the collection already holds.

    A label is a deliberate curation, so its roster is a structural neighbour
    rather than a guess about who else gets listened to.
    """
    expander = DiscogsLabelExpander(os.environ.get("DISCOGS_TOKEN", ""))
    reason = expander.unavailable_reason()
    if reason:
        typer.echo(reason, err=True)
        raise typer.Exit(code=2)

    with store.connect(settings) as driver:
        database = settings.neo4j_database
        candidates = [
            label
            for label in query.labels_to_expand(driver, database, min_artists, limit * 2)
            if is_real_label(label["key"])
        ][:limit]
        known = set(query.corpus_keys(driver, database))
    if not candidates:
        typer.echo("no labels yet: run `discolab enrich --discogs` first", err=True)
        raise typer.Exit(code=2)

    typer.echo(f"checking whether {len(candidates)} names are labels")
    targets = []
    for position, label in enumerate(candidates, start=1):
        if expander.is_label_entity(label["key"]):
            targets.append(label)
        if position % 10 == 0:
            typer.echo(f"  checked {position}/{len(candidates)}", err=True)
    typer.echo(f"{len(targets)} of {len(candidates)} are labels")

    typer.echo(f"expanding through {len(targets)} labels")
    seen: dict[tuple[str, str], dict] = {}
    crediting = 0
    new_artists = 0
    for position, label in enumerate(targets, start=1):
        for artist, _release in expander.artists_on(label["key"]):
            crediting += 1
            key = normalise(artist)
            if not key:
                continue
            corpus = key in known
            pair = (label["key"], key)
            if pair in seen:
                continue
            if not corpus:
                new_artists += 1
            seen[pair] = {
                "label_key": label["key"],
                "key": key,
                "name": artist,
                "external": not corpus,
            }
        if position % 10 == 0:
            typer.echo(f"  {position}/{len(targets)}", err=True)

    with store.connect(settings) as driver:
        load.write_label_mates(driver, settings.neo4j_database, list(seen.values()))
    typer.echo(f"credits seen: {crediting}")
    typer.echo(f"label and artist pairs written: {len(seen)}")
    typer.echo(f"artists not in your collection: {new_artists}")


@app.command()
def prune(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Remove artists and labels left with no relationships at all.

    A failed expansion or a cleared edge leaves nodes behind, and the
    enrichment walks them, spending API calls on names that cannot appear on a
    page because nothing connects to them.
    """
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        counts = query.orphan_counts(driver, database)
        if not any(counts.values()):
            typer.echo("nothing orphaned")
            return
        for label, count in counts.items():
            typer.echo(f"  {label}: {count}")
        if not yes:
            typer.confirm("delete them?", abort=True)
        removed = store.delete_orphans(driver, database)
    for label, count in removed.items():
        typer.echo(f"removed {count} {label}")


def _enrich_candidates_from_discogs(driver, database: str, keys: list[str]) -> None:
    """Ask Discogs only about the artists about to be shown.

    Covering the whole graph costs half an hour at one request a second, which
    is the same rate MusicBrainz offers. The candidates are twenty-five names,
    so the same enrichment is half a minute when it is asked for here.
    """
    missing = query.artists_without_discogs(driver, database, keys)
    if not missing:
        return
    enricher = DiscogsEnricher(os.environ.get("DISCOGS_TOKEN", ""))
    reason = enricher.unavailable_reason()
    if reason:
        typer.echo(reason, err=True)
        return

    typer.echo(f"asking Discogs about {len(missing)} artists")
    rows = []
    for position, artist in enumerate(missing, start=1):
        facts = enricher.facts(artist["name"])
        if facts is not None:
            rows.append(
                {
                    "key": artist["key"],
                    "styles": list(facts.styles),
                    "labels": list(facts.labels),
                }
            )
        if position % 10 == 0:
            typer.echo(f"  asked {position}/{len(missing)}", err=True)
    load.write_artist_sources(driver, database, rows)
    typer.echo(f"Discogs answered for {len(rows)} of {len(missing)}")


def _explain_empty(driver, database: str) -> None:
    summary = query.stats(driver, database)
    typer.echo("no candidates.")
    typer.echo(
        f"  artists {summary['artists']}, connected {summary['connected_artists']}, "
        f"isolated {summary['isolated_artists']}"
    )
    typer.echo(
        f"  islands {summary['components']}, largest {summary['largest_component']}"
    )
    typer.echo("  lower --min-degree, or add an expansion provider to connect islands")


TOKEN_PATH = Path("out/spotify-token.json")


@app.command("spotify-login")
def spotify_login(
    container: bool = typer.Option(
        False,
        "--container",
        help="Serve the callback from the compose service instead of this process.",
    ),
) -> None:
    """Authorise against Spotify once, then cache the refresh token."""
    settings = _settings([])
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        typer.echo("set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env", err=True)
        raise typer.Exit(code=2)

    redirect = settings.spotify_redirect_uri
    parsed = urllib.parse.urlparse(redirect)
    client = SpotifyClient(
        settings.spotify_client_id, settings.spotify_client_secret, TOKEN_PATH
    )

    if container:
        _login_via_container(client, redirect)
        return

    tls = None
    if settings.spotify_tls_cert and settings.spotify_tls_key:
        tls = (settings.spotify_tls_cert, settings.spotify_tls_key)
    if parsed.scheme == "https" and tls is None:
        typer.echo(
            "the redirect URI is https but SPOTIFY_TLS_CERT and SPOTIFY_TLS_KEY are "
            "unset. Use --container, or a loopback redirect URI which Spotify "
            "permits over http",
            err=True,
        )
        raise typer.Exit(code=2)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port < 1024:
        typer.echo(f"note: binding port {port} needs root", err=True)

    typer.echo(f"listening on {redirect}, a browser should open")
    try:
        client.login(redirect, tls)
    except SpotifyError as error:
        typer.echo(f"login failed: {error}", err=True)
        raise typer.Exit(code=3) from error
    typer.echo(f"logged in, refresh token cached in {TOKEN_PATH}")


def _login_via_container(client: SpotifyClient, redirect: str) -> None:
    """The callback runs as a compose service, so nothing serves on the host."""
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    url = client.build_authorize_url(redirect, state, verifier)

    parsed = urllib.parse.urlparse(redirect)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    command = [
        "docker", "compose", "--profile", "spotify", "run", "--rm",
        # compose run publishes nothing without this, so the browser could not
        # reach the callback.
        "--service-ports",
        "-e", f"SPOTIFY_STATE={state}",
        "-e", f"SPOTIFY_VERIFIER={verifier}",
        "-e", f"SPOTIFY_CALLBACK_PORT={port}",
        "spotify-callback",
    ]
    # Compose resolves the published port from its own environment rather than
    # from -e, so the mapping has to be told here too or it stays on the default.
    environment = dict(os.environ)
    environment["SPOTIFY_CALLBACK_PORT"] = str(port)

    typer.echo(f"starting the callback container, publishing port {port}")
    try:
        process = subprocess.Popen(command, env=environment)
    except FileNotFoundError:
        typer.echo("docker is not on PATH", err=True)
        raise typer.Exit(code=2) from None

    typer.echo("opening the authorization page")
    webbrowser.open(url)
    if process.wait() != 0:
        typer.echo("the callback container did not finish the exchange", err=True)
        raise typer.Exit(code=3)
    typer.echo(f"logged in, refresh token cached in {TOKEN_PATH}")


@app.command()
def check(
    limit: int = typer.Option(25, "--limit", help="Candidates to include."),
    min_degree: int = typer.Option(2, "--min-degree", help="Minimum artist degree."),
    tracks: int = typer.Option(6, "--tracks", help="Owned tracks to list per artist."),
    out: Path = typer.Option(Path("out/checklist.html"), "--out", help="Page to write."),
    youtube: bool = typer.Option(
        True,
        "--youtube/--no-youtube",
        help="Resolve one YouTube embed per artist. The only step that uses the network.",
    ),
    cache: Path = typer.Option(
        Path("out/videos.json"), "--cache", help="Resolved videos, reused to avoid re-searching."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore the cache and search again."
    ),
    votes: Path = typer.Option(
        Path("out/votes.json"), "--votes", help="Where the ticks are saved."
    ),
    source: str = typer.Option(
        "bridge",
        "--source",
        help="bridge ranks your own artists; external lists artists you do not have.",
    ),
    min_areas: int = typer.Option(
        0,
        "--min-areas",
        help="Require this many separate taste areas to agree. 2 is the useful setting.",
    ),
    genre: str = typer.Option(
        "",
        "--genre",
        help="Only count agreement from seeds tagged with one of these, comma separated. "
        "Applies to the ranking, not just to which seeds were expanded from.",
    ),
    discogs: bool = typer.Option(
        True,
        "--discogs/--no-discogs",
        help="Ask Discogs about the candidates for styles and labels. One request each.",
    ),
    serve: bool = typer.Option(
        True,
        "--serve/--no-serve",
        help="Serve the page on localhost, which the embeds require.",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the served page in a browser."
    ),
) -> None:
    """Write a local listening page, with a counter for the kill criterion."""
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        if source == "external":
            wanted = _genre_terms(genre)
            # Nothing here waits on a network call. Genre filtering reads what
            # the background enrichment has already written, and the enrichment
            # below only fills in the handful of artists actually shown.
            rows = query.external_candidates(
                driver, database, limit, min_areas, wanted
            )
        elif source == "labels":
            wanted = _genre_terms(genre)
            rows = query.label_candidates(driver, database, limit, wanted)
        else:
            rows = query.bridge_candidates(driver, database, limit, min_degree, 0.0)
        if not rows:
            _explain_empty(driver, database)
            if source == "external":
                typer.echo("  for external candidates run `discolab expand` first")
            raise typer.Exit(code=0)
        keys = [row["key"] for row in rows]
        owned = (
            query.owned_tracks_for(driver, database, keys, max(tracks, 40))
            if source == "bridge"
            else {}
        )
        titles = query.library_titles(driver, database)
        if discogs:
            _enrich_candidates_from_discogs(driver, database, keys)
        sources = query.artist_sources(driver, database, keys)
        tempo = query.artist_tempo(driver, database, keys)

    videos: dict[str, list[Video]] = {} if refresh else load_video_cache(cache)
    if youtube:
        pending = [row for row in rows if row["name"] not in videos]
        if videos:
            typer.echo(f"reusing {len(videos)} cached resolutions")
        for position, row in enumerate(pending, start=1):
            titles = [track["title"] for track in owned.get(row["key"], [])]
            videos[row["name"]] = resolve_artist(row["name"], titles)
            typer.echo(f"  resolved {position}/{len(pending)}", err=True)
            time.sleep(0.4)
        save_video_cache(cache, videos)
        with_track = sum(1 for row in rows if videos.get(row["name"]))
        typer.echo(f"artists with a track resolved: {with_track}/{len(rows)}")

    if source == "external" and wanted:
        typer.echo(f"genre filter: {', '.join(wanted[:4])}...")

    candidates = [
        Candidate(
            name=row["name"],
            community=row.get("community", 0),
            component=row.get("component", 0),
            component_size=row.get("component_size", 0),
            degree=row.get("degree", 0),
            bridge_score=row.get("best_score", row.get("bridge_score", 0.0)),
            videos=tuple(videos.get(row["name"], ())),
            tracks=tuple(owned.get(row["key"], [])[:tracks]),
            known_titles=tuple(
                (track["title"], track["plays"]) for track in owned.get(row["key"], [])
            ),
            seeds=tuple(row.get("seed_names") or row.get("via") or ()),
            external=source in ("external", "labels"),
            source_kind=source,
            seed_count=row.get("seed_count", row.get("labels", 0)),
            areas=row.get("areas", 0),
            styles=tuple(sources.get(row["key"], {}).get("styles", ())),
            labels=tuple(sources.get(row["key"], {}).get("labels", ())),
            bpm_low=tempo.get(row["key"], {}).get("low"),
            bpm_high=tempo.get(row["key"], {}).get("high"),
            musical_keys=tuple(tempo.get(row["key"], {}).get("musical_keys") or ()),
        )
        for row in rows
    ]
    path = write_page(build_page(candidates, titles, read_votes(votes)), out)
    typer.echo(f"wrote {path}")

    if not serve:
        typer.echo(f"open file://{path.resolve()}")
        if youtube:
            typer.echo("note: file:// pages cannot play the embeds, use --serve", err=True)
        return

    url, server = serve_directory(path.parent, path.name, votes_path=votes)
    typer.echo(f"serving {url}")
    typer.echo("ctrl-c to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        server.shutdown()


def _settings(roots: list[Path]) -> Settings:
    load_env_file()
    return Settings.from_env(tuple(roots) if roots else None)


if __name__ == "__main__":
    app()

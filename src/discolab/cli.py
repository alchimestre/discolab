from __future__ import annotations

import concurrent.futures
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
    build_cluster_index,
    build_page,
    load_video_cache,
    read_votes,
    resolve_artist,
    save_video_cache,
    serve_directory,
    write_page,
)
from .config import Settings, load_env_file
from .enrich import DeezerEnricher
from .rekordbox import RekordboxError, RekordboxReader, join_key
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
        True, "--lastfm/--no-lastfm", help="Read scrobbles. On unless switched off."
    ),
    spotify: bool = typer.Option(
        True,
        "--spotify/--no-spotify",
        help="Read playlists and liked songs. On unless switched off.",
    ),
    scrobbles: int = typer.Option(
        20000, "--scrobbles", help="Cap on scrobble history read."
    ),
    months: int = typer.Option(
        24,
        "--months",
        help="Only read the last N months of scrobbles, 0 for all history. Tastes move.",
    ),
    rekordbox: bool = typer.Option(
        True,
        "--rekordbox/--no-rekordbox",
        help="Take BPM, key and the DJ's tags from rekordbox. Local, and skipped "
        "when sqlcipher is absent.",
    ),
) -> None:
    """Read every configured source, then normalise it into the graph."""
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

    if lastfm and not (settings.lastfm_api_key and settings.lastfm_username):
        typer.echo("last.fm: skipped, set LASTFM_API_KEY and LASTFM_USERNAME", err=True)
        lastfm = False

    if lastfm:
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
            typer.echo("spotify: skipped, set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET", err=True)
            spotify = False
        else:
            client = SpotifyClient(
                settings.spotify_client_id, settings.spotify_client_secret, TOKEN_PATH
            )
            if not client.has_login:
                typer.echo("spotify: skipped, run `discolab spotify-login` first", err=True)
                spotify = False

    if spotify:
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

    if rekordbox:
        _apply_rekordbox_facts(settings)


def _apply_rekordbox_facts(settings: Settings) -> None:
    """Attach BPM, key and the DJ's tags from rekordbox, when it can be read.

    Local and free: no network, and no dependency beyond the sqlcipher binary
    that opens the encrypted database. Without it every other source stays.
    """
    reader = RekordboxReader()
    reason = reader.unavailable_reason()
    if reason:
        typer.echo(f"rekordbox: skipped, {reason}", err=True)
        return
    try:
        facts = reader.facts()
    except RekordboxError as error:
        typer.echo(f"rekordbox: skipped, {error}", err=True)
        return

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
    typer.echo(
        f"rekordbox: {len(rows)} of {len(pairs)} paths matched, "
        f"{sum(1 for row in rows if row['bpm'])} with a BPM, "
        f"{sum(1 for row in rows if row['tags'])} carrying tags"
    )


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
def guess(
    per_cluster: int = typer.Option(
        40, "--per-cluster", help="Most central members to take from each taste cluster."
    ),
    per_seed: int = typer.Option(
        100, "--per-seed", help="Similar artists to ask for per seed."
    ),
    isolated: int = typer.Option(
        400,
        "--isolated",
        help="Also seed from this many artists nothing else can reach, best known first.",
    ),
    limit: int = typer.Option(25, "--limit", help="Candidates on the page."),
    out: Path = typer.Option(Path("out/checklist.html"), "--out", help="Page to write."),
    youtube: bool = typer.Option(
        True, "--youtube/--no-youtube", help="Resolve one embed per candidate."
    ),
    serve: bool = typer.Option(
        True, "--serve/--no-serve", help="Serve the page, which the embeds require."
    ),
) -> None:
    """Derive taste communities, expand past them, write the listening page."""
    derive_edges(resolution=1.0, min_artists=2, max_artists=80)
    expand(
        per_cluster=per_cluster,
        per_seed=per_seed,
        min_degree=1,
        isolated=isolated,
        min_score=0.0,
    )
    # Similarity edges change who sits next to whom, so communities are
    # computed again before anything is ranked.
    derive_edges(resolution=1.0, min_artists=2, max_artists=80)
    check(
        limit=limit,
        min_degree=2,
        tracks=6,
        out=out,
        youtube=youtube,
        cache=Path("out/videos.json"),
        refresh=False,
        votes=Path("out/votes.json"),
        source="external",
        min_areas=0,
        cluster=-1,
        periphery=False,
        serve=serve,
        open_browser=serve,
    )


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
    min_score: float = typer.Option(
        0.0, "--min-score", help="Ignore matches weaker than this."
    ),
) -> None:
    """Expand from the centre of each taste cluster to artists you do not have."""
    settings = _settings([])
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
    external_only: bool = typer.Option(
        False, "--external-only", help="Limit the pass to artists from expansion."
    ),
    workers: int = typer.Option(
        10, "--workers", help="Parallel requests. Deezer measured 300 in 3.7 seconds."
    ),
    keep_going: bool = typer.Option(
        False, "--keep-going", help="Repeat until every artist has been asked about."
    ),
    min_agreement: int = typer.Option(
        0,
        "--min-agreement",
        help="Only ask about artists this many seeds point at. 0 walks everything.",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ask again about artists already done."
    ),
    limit: int = typer.Option(
        8000, "--limit", help="Artists to ask about; the next run continues where this stops."
    ),
) -> None:
    """Fetch genres and labels for artists, concurrently."""
    settings = _settings([])
    if refresh:
        with store.connect(settings) as driver:
            store.clear_enriched_flag(driver, settings.neo4j_database, external_only)
    _enrich_artists_with_deezer(
        settings, limit, external_only, keep_going, min_agreement, workers
    )


def _enrich_artists_with_deezer(
    settings: Settings,
    limit: int,
    external_only: bool,
    keep_going: bool = False,
    min_agreement: int = 2,
    workers: int = 8,
) -> None:
    """Fill in genres and labels for artists nobody has asked about yet.

    Concurrent because Deezer allows it, where Discogs allows one request a
    second and made the whole graph an hour of waiting.
    """
    enricher = DeezerEnricher()
    asked = 0
    answered = 0
    while True:
        with store.connect(settings) as driver:
            pending = query.artists_needing_genres(
                driver, settings.neo4j_database, limit, external_only, min_agreement
            )
        if not pending:
            break

        typer.echo(f"asking Deezer about {len(pending)} artists, {workers} at a time")
        rows: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            results = pool.map(lambda entry: enricher.facts(entry["name"]), pending)
            for artist, facts in zip(pending, results):
                asked += 1
                if facts is None:
                    # Record the miss so the next pass does not ask again.
                    rows.append({"key": artist["key"], "styles": [], "labels": []})
                else:
                    answered += 1
                    rows.append(
                        {
                            "key": artist["key"],
                            "styles": list(facts.genres),
                            "labels": list(facts.labels),
                        }
                    )
                if len(rows) >= 300:
                    with store.connect(settings) as driver:
                        load.write_artist_genres(driver, settings.neo4j_database, rows)
                    rows = []
                    typer.echo(f"  asked {asked}, answered {answered}", err=True)
        if rows:
            with store.connect(settings) as driver:
                load.write_artist_genres(driver, settings.neo4j_database, rows)
        if not keep_going:
            break

    typer.echo(f"Deezer asked about {asked}, answered for {answered}")


@app.command()
def clusters(
    limit: int = typer.Option(40, "--limit", help="Clusters to list."),
    pages: bool = typer.Option(
        False, "--pages", help="Write an index and one frontier page per cluster."
    ),
    per_cluster: int = typer.Option(12, "--per-cluster", help="Candidates per cluster page."),
    out: Path = typer.Option(Path("out"), "--out", help="Where the pages go."),
    youtube: bool = typer.Option(
        True, "--youtube/--no-youtube", help="Resolve an embed per candidate."
    ),
    cache: Path = typer.Option(
        Path("out/videos.json"), "--cache", help="Resolved videos, reused across runs."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the cache."),
) -> None:
    """List the taste clusters, and optionally build a page for each."""
    settings = _settings([])
    with store.connect(settings) as driver:
        database = settings.neo4j_database
        rows = query.clusters(driver, database, limit)
        if not rows:
            typer.echo("no clusters: run `discolab derive` first", err=True)
            raise typer.Exit(code=2)
        if not pages:
            for row in rows:
                typer.echo(f"{row['cluster']:>4}  {row['size']:>5}  {', '.join(row['styles'][:4])}")
                typer.echo(f"      {', '.join(row['members'][:5])}")
            return
        titles = query.library_titles(driver, database)
        videos = {} if refresh else load_video_cache(cache)
        built: list[int] = []
        for row in rows:
            cluster = row["cluster"]
            candidates = query.cluster_frontier(driver, database, cluster, per_cluster)
            if not candidates:
                continue
            if youtube:
                for candidate in candidates:
                    if candidate["name"] not in videos:
                        videos[candidate["name"]] = resolve_artist(candidate["name"])
                        time.sleep(0.4)
            keys = [c["key"] for c in candidates]
            sources = query.artist_sources(driver, database, keys)
            tempo = query.artist_tempo(driver, database, keys)
            votes = read_votes(out / "votes.json")
            nav = f'<p><a href="index.html">all clusters</a> · cluster {cluster}</p>'
            page = build_page(
                [
                    Candidate(
                        name=c["name"],
                        community=cluster,
                        component=0,
                        component_size=0,
                        degree=c.get("seed_count", 0),
                        bridge_score=c.get("best_score", 0.0),
                        videos=tuple(videos.get(c["name"], ())),
                        tracks=(),
                        known_titles=(),
                        seeds=tuple(c.get("seed_names") or ()),
                        external=True,
                        source_kind="cluster",
                        seed_count=c.get("seed_count", 0),
                        areas=1,
                        styles=tuple(sources.get(c["key"], {}).get("styles", ())),
                        labels=tuple(sources.get(c["key"], {}).get("labels", ())),
                        bpm_low=tempo.get(c["key"], {}).get("low"),
                        bpm_high=tempo.get(c["key"], {}).get("high"),
                        musical_keys=tuple(tempo.get(c["key"], {}).get("musical_keys") or ()),
                    )
                    for c in candidates
                ],
                titles,
                votes,
                title=f"cluster {cluster} frontier",
                nav=nav,
            )
            write_page(page, out / f"cluster-{cluster}.html")
            built.append(cluster)
        if youtube:
            save_video_cache(cache, videos)
        write_page(
            build_cluster_index(rows, built),
            out / "index.html",
        )
    typer.echo(f"wrote {out}/index.html and {len(built)} cluster pages")
    typer.echo(f"serve with: uv run discolab check --no-youtube --serve")


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
    cluster: int = typer.Option(
        -1,
        "--cluster",
        help="Scope the page to one cluster. -1 ranks across the whole taste.",
    ),
    periphery: bool = typer.Option(
        False,
        "--periphery",
        help="With --cluster, show that cluster's own weakly connected artists instead "
        "of its frontier.",
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
        if cluster >= 0 and periphery:
            rows = query.cluster_periphery(driver, database, cluster, limit)
            source = "bridge"
        elif cluster >= 0:
            rows = query.cluster_frontier(driver, database, cluster, limit)
            source = "external"
        elif source == "external":
            rows = query.external_candidates(driver, database, limit, min_areas)
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
            source_kind="cluster" if cluster >= 0 else source,
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

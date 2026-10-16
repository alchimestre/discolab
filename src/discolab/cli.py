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

    typer.echo(f"credit edges: {credits}, container edges: {containers}")
    typer.echo(
        f"graph: {summary['artists_in_graph']} artists, {summary['edges']} edges, "
        f"{summary['communities']} communities, {summary['components']} islands"
    )
    typer.echo(
        f"largest island: {summary['largest_component']} artists, "
        f"bridging artists: {summary['bridging_artists']}"
    )


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
    seeds: int = typer.Option(25, "--seeds", help="How many bridge artists to expand from."),
    per_seed: int = typer.Option(20, "--per-seed", help="Similar artists to ask for per seed."),
    min_degree: int = typer.Option(2, "--min-degree", help="Minimum seed degree."),
    min_score: float = typer.Option(
        0.0, "--min-score", help="Ignore matches weaker than this."
    ),
) -> None:
    """Expand from your bridge artists to artists you do not have."""
    settings = _settings([])
    if not settings.lastfm_api_key:
        typer.echo("set LASTFM_API_KEY in .env", err=True)
        raise typer.Exit(code=2)

    with store.connect(settings) as driver:
        database = settings.neo4j_database
        seed_rows = query.bridge_candidates(driver, database, seeds, min_degree, 0.0)
        known = set(query.artist_keys(driver, database))
    if not seed_rows:
        typer.echo("no bridge artists: run `discolab derive` first", err=True)
        raise typer.Exit(code=2)

    expander = LastfmSimilarExpander(settings.lastfm_api_key, per_seed)
    found = 0
    already = 0
    fresh: dict[str, dict] = {}
    for position, seed in enumerate(seed_rows, start=1):
        for suggestion in expander.similar(seed["name"]):
            found += 1
            key = normalise(suggestion.name)
            if not key:
                continue
            if key in known:
                already += 1
                continue
            if suggestion.score < min_score:
                continue
            current = fresh.get(key)
            if current is None or suggestion.score > current["score"]:
                fresh[key] = {
                    "key": key,
                    "name": suggestion.name,
                    "seed_key": seed["key"],
                    "seed": seed["name"],
                    "score": suggestion.score,
                    "source": expander.source,
                }
        typer.echo(f"  expanded {position}/{len(seed_rows)}", err=True)

    with store.connect(settings) as driver:
        written = load.write_expansion(driver, settings.neo4j_database, list(fresh.values()))

    share = f"{already / found:.0%}" if found else "n/a"
    typer.echo(f"suggestions from {len(seed_rows)} seeds: {found}")
    typer.echo(f"already in the corpus: {already} ({share})")
    typer.echo(f"new artists written: {written}")


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
            rows = query.external_candidates(driver, database, limit)
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
            seeds=tuple(row.get("seeds", ())),
            external=source == "external",
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

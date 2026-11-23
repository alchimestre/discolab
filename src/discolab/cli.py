from __future__ import annotations

import os

import secrets
import subprocess
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from .config import Settings, load_env_file
from .rekordbox import RekordboxError, RekordboxReader, join_key
from .expand import LastfmSimilarExpander
from .webapp import make_server
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
from .tags import LastfmTagger, TagError

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

    with store.connect(settings) as connection:
        if recreate:
            store.clear(connection)
        load.load_catalog(connection, catalog)
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

    with store.connect(settings) as connection:
        pairs = query.track_paths(connection)
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
        load.write_track_facts(connection, rows)
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
    with store.connect(settings) as connection:
        credits = derive.build_collaboration_edges(connection)
        containers = derive.build_container_edges(connection, min_artists, max_artists)
        summary = derive.assign_communities(connection, resolution)
        areas = derive.assign_areas(connection)

    typer.echo(f"credit edges: {credits}, container edges: {containers}")
    typer.echo(
        f"graph: {summary['artists_in_graph']} artists, {summary['edges']} edges, "
        f"{summary['communities']} communities, {summary['components']} islands"
    )
    typer.echo(
        f"largest island: {summary['largest_component']} artists, "
        f"bridging artists: {summary['bridging_artists']}"
    )
    typer.echo(f"taste areas over your own artists: {areas}")


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
    with store.connect(settings) as connection:
        rows = query.bridge_candidates(connection, limit, min_degree, min_bridge)
        if not rows:
            _explain_empty(connection)
            raise typer.Exit(code=0)
        owned = (
            query.owned_tracks_for(connection, [row["key"] for row in rows])
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
    with store.connect(settings) as connection:
        summary = query.stats(connection)
        island_rows = query.islands(connection, island_count)
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")
    typer.echo("islands (largest first):")
    for island in island_rows:
        typer.echo(f"  {island['size']:4}  {', '.join(island['members'])}")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt.")) -> None:
    """Delete every row in the database."""
    settings = _settings([])
    if not yes:
        typer.confirm(f"delete all data in {settings.database_path}?", abort=True)
    with store.connect(settings) as connection:
        store.clear(connection)
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
    serve_app: bool = typer.Option(
        True, "--serve/--no-serve", help="Open the app once the graph is ready."
    ),
) -> None:
    """Derive taste communities, expand past them, open the app."""
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
    if serve_app:
        serve(port=DEFAULT_PORT, open_browser=True)


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

    with store.connect(settings) as connection:
        seed_rows = query.seed_artists(connection, per_cluster, min_degree, isolated)
        known = set(query.corpus_keys(connection))
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
    dirty: list[tuple[str, str]] = []
    # Flushed as it goes. Held to the end, a run that takes twenty minutes shows
    # an unchanged graph until it finishes, which looks the same as being stuck.
    with store.connect(settings) as connection:
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
                        "url": suggestion.url or None,
                        "source": expander.source,
                        "external": not corpus,
                    }
                    dirty.append(pair)
            if position % 100 == 0:
                load.write_expansion(connection, [fresh[pair] for pair in dirty])
                dirty = []
                typer.echo(f"  expanded {position}/{len(seed_rows)}", err=True)
        load.write_expansion(connection, [fresh[pair] for pair in dirty])

    share = f"{linked / found:.0%}" if found else "n/a"
    fresh_count = sum(1 for row in fresh.values() if row["external"])
    with store.connect(settings) as connection:
        dropped = load.drop_single_seed_artists(connection)

    typer.echo(f"suggestions from {len(seed_rows)} seeds: {found}")
    typer.echo(f"links between your own artists: {linked} ({share})")
    typer.echo(f"new artists written: {fresh_count}")
    typer.echo(f"dropped {dropped} that only one seed named, which cannot reach a page")


@app.command()
def clusters(
    limit: int = typer.Option(40, "--limit", help="Clusters to list."),
) -> None:
    """List the taste clusters."""
    settings = _settings([])
    with store.connect(settings) as connection:
        rows = query.clusters(connection, limit)
        if not rows:
            typer.echo("no clusters: run `discolab derive` first", err=True)
            raise typer.Exit(code=2)
        for row in rows:
            typer.echo(f"{row['cluster']:>4}  {row['size']:>5}  {', '.join(row['styles'][:4])}")
            typer.echo(f"      {', '.join(row['members'][:5])}")


@app.command()
def prune(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Remove artists left with no tracks, credits, tags or edges.

    A failed expansion or a cleared edge leaves nodes behind, and the
    enrichment walks them, spending API calls on names that cannot appear on a
    page because nothing connects to them.
    """
    settings = _settings([])
    with store.connect(settings) as connection:
        orphans = store.orphan_count(connection)
        if not orphans:
            typer.echo("nothing orphaned")
            return
        typer.echo(f"  artists: {orphans}")
        if not yes:
            typer.confirm("delete them?", abort=True)
        removed = store.delete_orphans(connection)
    typer.echo(f"removed {removed} artists")


def _explain_empty(connection) -> None:
    summary = query.stats(connection)
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


DEFAULT_PORT = 8765


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Port on localhost."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the app in a browser."
    ),
) -> None:
    """Serve the discoveries, the area map and the area pages on localhost."""
    server = make_server(_settings([]), port)
    url = f"http://127.0.0.1:{server.server_port}/"
    typer.echo(f"serving {url}")
    typer.echo("ctrl-c to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        server.server_close()


@app.command()
def tags(
    limit: int = typer.Option(
        0, "--limit", help="Stop after this many artists. 0 walks the whole gap."
    ),
    rate: float = typer.Option(4.0, "--rate", help="Requests per second."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Ask, report, write nothing."),
) -> None:
    """Fill in Last.fm's genre tags where no genre source has one."""
    settings = _settings([])
    if not settings.lastfm_api_key:
        typer.echo("set LASTFM_API_KEY in .env", err=True)
        raise typer.Exit(code=2)

    with store.connect(settings) as connection:
        rows = connection.execute(
            """
            SELECT key, name FROM artist
            WHERE community >= 0 AND styles = '[]' AND lastfm = 0
            ORDER BY degree DESC, name
            """
        ).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        typer.echo("nothing to ask about: every clustered artist has a style already")
        return

    tagger = LastfmTagger(settings.lastfm_api_key, rate)
    typer.echo(f"asking Last.fm about {len(rows):,} artists", err=True)

    pending: list[dict] = []
    found = tagged = failed = 0
    # Flushed as it goes. A run of twelve thousand requests held to the end shows
    # an unchanged graph for an hour and loses everything if it stops.
    with store.connect(settings) as connection:
        for position, row in enumerate(rows, start=1):
            try:
                names = tagger.tags(row["name"])
            except TagError as error:
                failed += 1
                if failed <= 3:
                    typer.echo(f"  {error}", err=True)
                names = []
            found += 1
            if names:
                tagged += 1
            # Every artist asked is written, even with nothing to attach. The
            # writer marks them, and an artist left unmarked is asked again on
            # every later run, forever, because nothing about it changed.
            pending.append({"key": row["key"], "styles": names})
            if len(pending) >= 200:
                if not dry_run:
                    load.write_lastfm_tags(connection, pending)
                pending = []
            if position % 500 == 0:
                typer.echo(f"  {position}/{len(rows)} · {tagged} with tags", err=True)
        if pending and not dry_run:
            load.write_lastfm_tags(connection, pending)

    typer.echo(
        f"asked {found:,} · {tagged:,} had genre tags · {failed:,} failed"
        + (" (dry run, nothing written)" if dry_run else "")
    )


def _settings(roots: list[Path]) -> Settings:
    load_env_file()
    return Settings.from_env(tuple(roots) if roots else None)


if __name__ == "__main__":
    app()

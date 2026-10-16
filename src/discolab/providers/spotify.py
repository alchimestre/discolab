"""Spotify as a seed provider.

The authorization code flow, with a callback server that can terminate TLS for
a real hostname or serve plain HTTP on loopback, which Spotify also permits.
"""

from __future__ import annotations

import base64
import http.server
import json
import secrets
import ssl
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..model import RawTrack
from .base import ScanResult

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ROOT = "https://api.spotify.com/v1"

SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)

LOGIN_TIMEOUT = 300
PAGE_SIZE = 100


class SpotifyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Tokens:
    refresh_token: str
    access_token: str = ""
    expires_at: float = 0.0


class SpotifyClient:
    """Talks to the Web API. Holds the refresh token so login happens once."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self._tokens = _read_tokens(token_path)

    @property
    def has_login(self) -> bool:
        return bool(self._tokens and self._tokens.refresh_token)

    def build_authorize_url(self, redirect_uri: str, state: str, verifier: str) -> str:
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                # Spotify accepts PKCE alongside the secret, and it binds the
                # code to the process that asked for it.
                "code_challenge_method": "S256",
                "code_challenge": _challenge(verifier),
            }
        )

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Tokens:
        payload = self._post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        refresh = payload.get("refresh_token")
        if not refresh:
            raise SpotifyError("no refresh token returned")
        tokens = Tokens(refresh_token=refresh)
        _write_tokens(self.token_path, tokens)
        return tokens

    def login(self, redirect_uri: str, tls: tuple[str, str] | None = None) -> None:
        parsed = urllib.parse.urlparse(redirect_uri)
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)

        listener = _CallbackListener(parsed, tls)
        try:
            listener.start()
            webbrowser.open(self.build_authorize_url(redirect_uri, state, verifier))
            query = listener.wait(LOGIN_TIMEOUT)
        finally:
            listener.stop()

        if query.get("state") != state:
            raise SpotifyError("state mismatch, the callback is not from this login")
        if "error" in query:
            raise SpotifyError(f"authorization refused: {query['error']}")
        code = query.get("code")
        if not code:
            raise SpotifyError("no code in the callback")
        self._tokens = self.exchange_code(code, verifier, redirect_uri)

    def token(self) -> str:
        if self._tokens is None:
            raise SpotifyError("not logged in, run `discolab spotify-login` first")
        if self._tokens.access_token and time.time() < self._tokens.expires_at - 30:
            return self._tokens.access_token
        payload = self._post(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": self._tokens.refresh_token},
        )
        access = payload.get("access_token")
        if not access:
            raise SpotifyError("refresh failed, run `discolab spotify-login` again")
        self._tokens = Tokens(
            refresh_token=self._tokens.refresh_token,
            access_token=access,
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
        )
        return access

    def get(self, path: str, **params) -> dict:
        url = f"{API_ROOT}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token()}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:200]
            raise SpotifyError(f"{path}: HTTP {error.code} {body}") from error
        except Exception as error:
            raise SpotifyError(f"{path}: {error}") from error

    def _post(self, url: str, data: dict) -> dict:
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise SpotifyError(f"token request failed: HTTP {error.code} {body}") from error


class SpotifyProvider:
    """Reads playlists and liked songs, which is the ownership signal the files lack."""

    name = "spotify"

    def __init__(self, client: SpotifyClient, playlist_limit: int = 200):
        self.client = client
        self.playlist_limit = playlist_limit

    def scan(self) -> ScanResult:
        tracks: dict[tuple[str, str], RawTrack] = {}
        skipped: list[tuple[str, str]] = []

        containers: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for name, entries in self._sources():
            for artist, title, album in entries:
                key = (artist, title)
                if not artist or not title:
                    skipped.append((f"{artist} - {title}", "missing artist or title"))
                    continue
                containers.setdefault(key, []).append(("playlist", name))
                if key not in tracks:
                    tracks[key] = RawTrack(
                        source=self.name,
                        path="",
                        artist_raw=artist,
                        title_raw=title,
                        album_raw=album,
                        duration_ms=None,
                        containers=(),
                    )

        result = []
        for key, track in tracks.items():
            result.append(
                RawTrack(
                    source=track.source,
                    path=track.path,
                    artist_raw=track.artist_raw,
                    title_raw=track.title_raw,
                    album_raw=track.album_raw,
                    duration_ms=track.duration_ms,
                    containers=tuple(containers.get(key, ())),
                )
            )
        return ScanResult(source=self.name, tracks=tuple(result), skipped=tuple(skipped))

    def _sources(self):
        """Yield (name, entries) where an entry is artist, title, album."""
        yield "Liked Songs", self._liked()
        for playlist in self._playlists():
            yield playlist["name"], self._playlist_tracks(playlist["id"])

    def _playlists(self) -> list[dict]:
        playlists: list[dict] = []
        offset = 0
        while len(playlists) < self.playlist_limit:
            page = self.client.get("/me/playlists", limit=50, offset=offset)
            items = page.get("items") or []
            playlists.extend(item for item in items if item)
            if len(items) < 50:
                break
            offset += 50
        return playlists[: self.playlist_limit]

    def _liked(self) -> list[tuple[str, str, str | None]]:
        entries: list[tuple[str, str, str | None]] = []
        offset = 0
        while True:
            page = self.client.get("/me/tracks", limit=50, offset=offset)
            items = page.get("items") or []
            for item in items:
                track = (item or {}).get("track") or {}
                entries.append(_entry(track))
            if len(items) < 50:
                break
            offset += 50
        return entries

    def _playlist_tracks(self, playlist_id: str) -> list[tuple[str, str, str | None]]:
        entries: list[tuple[str, str, str | None]] = []
        offset = 0
        while True:
            page = self.client.get(
                f"/playlists/{playlist_id}/tracks",
                limit=PAGE_SIZE,
                offset=offset,
                additional_types="track",
            )
            items = page.get("items") or []
            for item in items:
                track = (item or {}).get("track") or {}
                entries.append(_entry(track))
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return entries


def _entry(track: dict) -> tuple[str, str, str | None]:
    artists = track.get("artists") or []
    artist = artists[0].get("name", "") if artists else ""
    album = (track.get("album") or {}).get("name")
    return artist, track.get("name", ""), album


def _challenge(verifier: str) -> str:
    import hashlib

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _read_tokens(path: Path) -> Tokens | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    refresh = data.get("refresh_token")
    return Tokens(refresh_token=refresh) if refresh else None


def _write_tokens(path: Path, tokens: Tokens) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"refresh_token": tokens.refresh_token}, indent=1), encoding="utf-8"
    )
    path.chmod(0o600)


class _CallbackListener:
    """Serves one request on the redirect URI's path and hands back its query.

    Binding loopback keeps the callback off the network when the flow runs on
    the host, but inside a container loopback is unreachable through a
    published port, so the container binds all interfaces instead.
    """

    def __init__(
        self,
        target: urllib.parse.ParseResult,
        tls: tuple[str, str] | None,
        host: str = "127.0.0.1",
    ):
        self.target = target
        self.tls = tls
        self.host = host
        self.port = target.port or (443 if target.scheme == "https" else 80)
        self.path = target.path or "/"
        self.query: dict[str, str] = {}
        self.done = threading.Event()
        self.server: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != listener.path:
                    self.send_error(404)
                    return
                listener.query = dict(urllib.parse.parse_qsl(parsed.query))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h1>discolab</h1><p>Login complete. Close this tab.</p>"
                )
                listener.done.set()

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        if self.tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.tls[0], self.tls[1])
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, timeout: int) -> dict[str, str]:
        if not self.done.wait(timeout):
            raise SpotifyError(f"no callback within {timeout}s")
        return self.query

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()

"""The Spotify OAuth callback, runnable on its own so it can be containerised.

The host process builds the authorize URL and hands this the state and the PKCE
verifier; this serves the redirect, exchanges the code, writes the refresh
token where the host reads it, and exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .providers.spotify import SpotifyClient, SpotifyError, _CallbackListener
import urllib.parse

# What the callback needs, all supplied by the host through the environment.
REQUIRED = (
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
    "SPOTIFY_STATE",
    "SPOTIFY_VERIFIER",
)


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2

    redirect = os.environ["SPOTIFY_REDIRECT_URI"]
    state = os.environ["SPOTIFY_STATE"]
    verifier = os.environ["SPOTIFY_VERIFIER"]
    token_path = Path(os.environ.get("SPOTIFY_TOKEN_PATH", "out/spotify-token.json"))
    cert = os.environ.get("SPOTIFY_TLS_CERT")
    key = os.environ.get("SPOTIFY_TLS_KEY")
    timeout = int(os.environ.get("SPOTIFY_LOGIN_TIMEOUT", "300"))

    parsed = urllib.parse.urlparse(redirect)
    if parsed.scheme == "https" and not (cert and key):
        print("https redirect needs SPOTIFY_TLS_CERT and SPOTIFY_TLS_KEY", file=sys.stderr)
        return 2

    listener = _CallbackListener(
        parsed,
        (cert, key) if cert and key else None,
        host=os.environ.get("SPOTIFY_BIND_HOST", "0.0.0.0"),
    )
    client = SpotifyClient(os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"], token_path)
    try:
        listener.start()
        print(f"callback listening on {redirect}", flush=True)
        query = listener.wait(timeout)
    except SpotifyError as error:
        print(f"callback failed: {error}", file=sys.stderr)
        return 3
    finally:
        listener.stop()

    if query.get("state") != state:
        print("state mismatch, ignoring the callback", file=sys.stderr)
        return 3
    if "error" in query:
        print(f"authorization refused: {query['error']}", file=sys.stderr)
        return 3
    code = query.get("code")
    if not code:
        print("no code in the callback", file=sys.stderr)
        return 3

    try:
        client.exchange_code(code, verifier, redirect)
    except SpotifyError as error:
        print(f"exchange failed: {error}", file=sys.stderr)
        return 3
    print(f"refresh token written to {token_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

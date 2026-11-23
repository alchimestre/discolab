"""The local app: discoveries, the area map and area pages, read live from the database."""

from __future__ import annotations

import functools
import http.server
import json
import sqlite3
import threading
import urllib.parse
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .graph import query, store
from .map_view import Area, layout, radius_for
from .youtube import load_video_cache, resolve_artist, save_video_cache

STATIC = Path(__file__).parent / "static"
HOST = "127.0.0.1"
# SQLite reads a negative LIMIT as no limit.
EVERY = -1


def make_server(settings: Settings, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(
        _Handler,
        settings=settings,
        videos=Videos(settings.database_path.parent / "videos.json"),
    )
    return http.server.ThreadingHTTPServer((HOST, port), handler)


def graph_payload(connection: sqlite3.Connection) -> dict:
    """Everything the pages draw: the areas, placed, and every discovery between them."""
    discoveries = query.external_candidates(connection, EVERY)
    shared = Counter(
        tuple(sorted(vote["area"] for vote in discovery["votes"]))
        for discovery in discoveries
    )
    rows = query.areas(connection)
    largest = max((len(row["artists"]) for row in rows), default=1)
    placed = [
        Area(
            key=row["area"],
            size=len(row["artists"]),
            genres=tuple(row["styles"]),
            r=radius_for(len(row["artists"]), largest),
        )
        for row in rows
    ]
    layout(placed, [(a, b, count) for (a, b), count in shared.items()])
    return {
        "areas": [_area_json(area, row) for area, row in zip(placed, rows)],
        "links": [{"a": a, "b": b, "shared": count} for (a, b), count in shared.items()],
        "discoveries": discoveries,
    }


def _area_json(area: Area, row: dict) -> dict:
    return {
        "id": area.key,
        "x": round(area.x, 1),
        "y": round(area.y, 1),
        "r": round(area.r, 1),
        "colour": area.colour,
        "family": area.family,
        "styles": [style for style, _ in row["styles"][:5]],
        "artists": row["artists"],
    }


class Videos:
    """Official uploads per artist, searched once and kept on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.found = load_video_cache(path)
        self.lock = threading.Lock()

    def of(self, name: str) -> list[dict]:
        with self.lock:
            if name not in self.found:
                videos = resolve_artist(name)
                # A failed search looks the same as no upload, so it is not remembered.
                if not videos:
                    return []
                self.found[name] = videos
                save_video_cache(self.path, self.found)
            return [asdict(video) for video in self.found[name]]


class _Handler(http.server.SimpleHTTPRequestHandler):
    # Set before the base class runs: its constructor is what handles the request.
    def __init__(self, *args, settings: Settings, videos: Videos, **kwargs) -> None:
        self.settings = settings
        self.videos = videos
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/api/graph":
            with store.connect(self.settings) as connection:
                self._json(graph_payload(connection))
        elif url.path == "/api/videos":
            self._videos(urllib.parse.parse_qs(url.query).get("name", [""])[0])
        else:
            super().do_GET()

    def _videos(self, name: str) -> None:
        with store.connect(self.settings) as connection:
            known = connection.execute(
                "SELECT 1 FROM artist WHERE name = ?", (name,)
            ).fetchone()
        if known is None:
            self.send_error(404, "no such artist")
            return
        self._json(self.videos.of(name))

    def _json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Only successful requests are silenced; errors still reach the terminal.
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

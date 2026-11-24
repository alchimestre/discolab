"""The local app: discoveries, the area map and area pages, read live from the database."""

from __future__ import annotations

import functools
import http.server
import itertools
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
EVERY = -1
MIN_STYLE_HOLDERS = 3
MIN_STYLE_SHARE = 0.1


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
        pair
        for discovery in discoveries
        for pair in itertools.combinations(sorted(vote["area"] for vote in discovery["votes"]), 2)
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
    links = [(a, b, count) for (a, b), count in shared.items()]
    layout(placed, links + _sibling_pulls(rows, max(shared.values(), default=1)))
    tints = _region_tints(rows)
    named = _distinctive_styles(rows)
    return {
        "areas": [
            _area_json(area, row, tints[row["region"]], named[row["area"]])
            for area, row in zip(placed, rows)
        ],
        "links": [{"a": a, "b": b, "shared": count} for a, b, count in links],
        "discoveries": discoveries,
    }


def _sibling_pulls(rows: list[dict], weight: int) -> list[tuple[int, int, int]]:
    """A link at full weight between every two areas of one region, so a region is drawn together."""
    return [
        (a["area"], b["area"], weight)
        for a, b in itertools.combinations(rows, 2)
        if a["region"] == b["region"]
    ]


def _region_tints(rows: list[dict]) -> dict[int, Area]:
    """One genre family per region, from the styles of all its areas."""
    styles: dict[int, Counter[str]] = {}
    for row in rows:
        styles.setdefault(row["region"], Counter()).update(dict(row["styles"]))
    return {
        region: Area(key=region, size=0, genres=tuple(counts.most_common()))
        for region, counts in styles.items()
    }


def _distinctive_styles(rows: list[dict]) -> dict[int, list[str]]:
    """Each area's styles, the ones most over-represented against every area first.

    Ranked by count, a dozen areas are all "Electro"; ranked by lift they read
    techno, ambient, psytrance. A style held by too few members to name the
    area is left out, and a case-only duplicate ("Jazz", "jazz") is shown once.
    """
    everywhere: Counter[str] = Counter()
    for row in rows:
        everywhere.update(dict(row["styles"]))
    artists = sum(len(row["artists"]) for row in rows)
    named = {}
    for row in rows:
        size = len(row["artists"])
        held = max(MIN_STYLE_HOLDERS, MIN_STYLE_SHARE * size)
        lift = {
            style: (count / size) / (everywhere[style] / artists)
            for style, count in row["styles"]
            if count >= held
        }
        ordered = sorted(lift, key=lambda style: -lift[style])
        ordered += [style for style, _ in row["styles"] if style not in lift]
        first_spelling: dict[str, str] = {}
        for style in ordered:
            first_spelling.setdefault(style.casefold(), style)
        named[row["area"]] = list(first_spelling.values())
    return named


def _area_json(area: Area, row: dict, region: Area, styles: list[str]) -> dict:
    return {
        "id": area.key,
        "region": row["region"],
        "x": round(area.x, 1),
        "y": round(area.y, 1),
        "r": round(area.r, 1),
        "colour": region.colour,
        "family": region.family,
        "styles": styles[:5],
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
        self.end_headers()
        self.wfile.write(body)

    # Without this a browser pairs a new index.html with the app.js it cached.
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # Only successful requests are silenced; errors still reach the terminal.
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

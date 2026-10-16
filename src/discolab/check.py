"""Build a local listening page for the candidates.

The page is the instrument for the kill criterion: listen, tick what is worth
playing again, read the count.
"""

from __future__ import annotations

import functools
import html
import http.server
import json
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .resolve import normalise

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SEARCH_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class Video:
    video_id: str
    title: str
    channel: str = ""

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube.com/embed/{self.video_id}"


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    community: int
    component: int
    component_size: int
    degree: int
    bridge_score: float
    tracks: tuple[dict, ...]


def search_videos(query: str, timeout: float = SEARCH_TIMEOUT) -> list[Video]:
    """Search YouTube without an API key or an account."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception:
        return []
    return _videos(body)


def resolve_artist(name: str) -> Video | None:
    """Find a video for an artist that is likely to play.

    A plain name search returns live sets, re-uploads and unrelated channels
    first, and a dead embed only shows up as a dead player. Prefer a video that
    names the artist, then fall back to whatever came first.
    """
    fallback: Video | None = None
    for query in (f"{name} - Topic", name):
        for video in search_videos(query):
            if _names_artist(video, name):
                return video
            if fallback is None:
                fallback = video
    return fallback


def _names_artist(video: Video, artist: str) -> bool:
    """True when the artist's name is in the channel, or credited in a Topic title.

    Matching the title alone is not enough: it accepts any upload whose track
    happens to share the artist's name. Channel names ending in 'Topic' exist
    for every credited person, so a title match is only trusted there.
    """
    key = normalise(artist)
    if not key:
        return False
    channel = normalise(video.channel)
    if channel.endswith("topic"):
        channel = channel[: -len("topic")].strip()
        return key in channel or key in normalise(video.title)
    return key in channel


def _videos(body: str) -> list[Video]:
    match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", body, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            data = None
        if data is not None:
            found: list[Video] = []
            _collect_renderers(data, found)
            if found:
                return found
    fallback = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', body)
    return [Video(fallback.group(1), "")] if fallback else []


def _collect_renderers(node, found: list[Video], limit: int = 30) -> None:
    if len(found) >= limit:
        return
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict) and renderer.get("videoId"):
            found.append(
                Video(
                    renderer["videoId"],
                    _renderer_title(renderer),
                    _renderer_channel(renderer),
                )
            )
            return
        for value in node.values():
            _collect_renderers(value, found, limit)
            if len(found) >= limit:
                return
    elif isinstance(node, list):
        for item in node:
            _collect_renderers(item, found, limit)
            if len(found) >= limit:
                return


def _renderer_channel(renderer: dict) -> str:
    owner = renderer.get("ownerText") or renderer.get("longBylineText") or {}
    runs = owner.get("runs")
    if isinstance(runs, list) and runs:
        return runs[0].get("text", "")
    return ""


def _renderer_title(renderer: dict) -> str:
    title = renderer.get("title") or {}
    runs = title.get("runs")
    if isinstance(runs, list) and runs:
        return runs[0].get("text", "")
    return title.get("simpleText", "")


def build_page(
    candidates: Sequence[Candidate],
    videos: dict[str, Video | None],
    title: str = "discolab checklist",
) -> str:
    sections = "\n".join(
        _section(index, candidate, videos.get(candidate.name))
        for index, candidate in enumerate(candidates, start=1)
    )
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 17px; margin: 32px 0 4px; }}
  h2 small {{ font-weight: 400; opacity: .65; font-size: 13px; }}
  ul {{ margin: 6px 0 0; padding-left: 20px; }}
  li {{ font-size: 13px; opacity: .85; }}
  iframe {{ width: 100%; max-width: 560px; aspect-ratio: 16/9; border: 0; margin-top: 8px; }}
  .links a {{ font-size: 13px; margin-right: 10px; }}
  .video-title {{ font-size: 13px; opacity: .7; margin: 4px 0 0; }}
  .keep {{ margin-top: 10px; display: inline-block; font-size: 14px; }}
  #bar {{ position: sticky; top: 0; background: Canvas; padding: 8px 0;
         border-bottom: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }}
  #count {{ font-weight: 600; }}
  .killed {{ opacity: .45; }}
</style>
<div id="bar">Kept <span id="count">0 of {len(candidates)}</span>. Criterion: fewer than 3 kept means the query is wrong, the data is too thin, or blank space is not fertile.</div>
<h1>{html.escape(title)}</h1>
<p>One embed per artist, plus links to the other catalogues. Tick the ones worth playing again. If a player says the video is unavailable, the uploader blocked embedding: use the YouTube link.</p>
{sections}
<script>
  const boxes = [...document.querySelectorAll('input.keep')];
  const label = document.getElementById('count');
  function update() {{
    const kept = boxes.filter(b => b.checked);
    label.textContent = kept.length + ' of ' + boxes.length;
    boxes.forEach(b => b.closest('section').classList.toggle('killed',
      boxes.some(o => o.checked) && !b.checked));
  }}
  boxes.forEach(b => b.addEventListener('change', update));
  update();
</script>
</html>
"""


def _section(index: int, candidate: Candidate, video: Video | None) -> str:
    query = urllib.parse.quote(candidate.name)
    parts = [
        f"<section>",
        f"<h2>{index}. {html.escape(candidate.name)} "
        f"<small>island {candidate.component} of {candidate.component_size} artists, "
        f"degree {candidate.degree}, bridge {candidate.bridge_score:.5f}</small></h2>",
        '<p class="links">'
        f'<a href="https://open.spotify.com/search/{query}">Spotify</a>'
        f'<a href="https://www.youtube.com/results?search_query={query}">YouTube</a>'
        f'<a href="https://bandcamp.com/search?q={query}">Bandcamp</a>'
        f'<a href="https://www.discogs.com/search/?q={query}&type=artist">Discogs</a>'
        f'<a href="https://musicbrainz.org/search?query={query}&type=artist">MusicBrainz</a>'
        "</p>",
    ]
    if video is not None:
        parts.append(
            f'<iframe src="{video.embed_url}" loading="lazy" '
            'allow="accelerometer; clipboard-write; encrypted-media; picture-in-picture" '
            "allowfullscreen></iframe>"
        )
        if video.title:
            source = f"{video.title} · {video.channel}" if video.channel else video.title
            parts.append(f'<p class="video-title">{html.escape(source)}</p>')
    else:
        parts.append(
            '<p class="video-title">no embed resolved, use the YouTube search link above</p>'
        )
    if candidate.tracks:
        items = "".join(
            f"<li>{html.escape(track['title'])} &middot; {html.escape(track['path'])}</li>"
            for track in candidate.tracks
        )
        parts.append(f"<ul>{items}</ul>")
    parts.append('<label class="keep"><input type="checkbox" class="keep"> keep</label>')
    parts.append("</section>")
    return "\n".join(parts)


def write_page(content: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve_directory(
    directory: Path, filename: str, host: str = "127.0.0.1"
) -> tuple[str, http.server.ThreadingHTTPServer]:
    """Serve the page over http, not file.

    YouTube returns error 153 for embeds loaded from file:// because the
    request carries no referrer.
    """
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{host}:{server.server_port}/{filename}", server

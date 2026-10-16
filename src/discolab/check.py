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
MAX_VIDEOS_PER_ARTIST = 4

# A set, mix or full album upload is not what a name search should return first.
SET_PATTERNS = re.compile(
    r"boiler room|cercle|essential mix|\bdj set\b|\blive set\b|\bfull set\b|full album|"
    r"mix vol|mixtape|megamix|compilation|podcast|continuous mix|best of|\bbest-of\b|"
    r"live @|live at |radio show|resident advisor|set movie|aftermovie|album complet|"
    r"álbum completo|\bfull movie\b|\bselected works\b|\bplayed by\b|\bcelebration mix\b",
    re.IGNORECASE,
)

# Sets run long. Individual tracks rarely pass this, so it separates the two
# without needing to know what the video is.
MAX_TRACK_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class Video:
    video_id: str
    title: str
    channel: str = ""
    duration_seconds: int | None = None
    verified: bool = False

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube.com/embed/{self.video_id}?enablejsapi=1"

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def thumbnail_url(self) -> str:
        return f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"

    @property
    def duration_label(self) -> str:
        if self.duration_seconds is None:
            return ""
        minutes, seconds = divmod(self.duration_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def label(self) -> str:
        body = " · ".join(part for part in (self.title, self.channel) if part)
        body = body or self.video_id
        return f"{self.duration_label} {body}".strip()


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    community: int
    component: int
    component_size: int
    degree: int
    bridge_score: float
    videos: tuple[Video, ...]
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


def resolve_artist(name: str, limit: int = MAX_VIDEOS_PER_ARTIST) -> list[Video]:
    """Ranked official tracks for an artist, best first.

    Only official uploads are returned. Several are returned because uploaders
    can still block embedding, and a blocked upload only reveals itself as a
    dead player. An artist with no official upload yields nothing, which is
    better than embedding a fan re-upload.
    """
    found: list[Video] = []
    seen: set[str] = set()
    for query in (f"{name} - Topic", name):
        for video in search_videos(query):
            if video.video_id in seen:
                continue
            seen.add(video.video_id)
            if not is_track(video) or not is_official(video, name):
                continue
            found.append(video)
        if found:
            break
    return found[:limit]


def is_track(video: Video) -> bool:
    """True for a single track rather than a set, mix or full album upload."""
    if SET_PATTERNS.search(video.title):
        return False
    if video.duration_seconds is not None and video.duration_seconds > MAX_TRACK_SECONDS:
        return False
    return True


def is_official(video: Video, artist: str) -> bool:
    """True for the artist's own channel, their distributor, or a verified channel.

    Fan re-uploads and aggregator channels are the common case in a name
    search, so an unverified channel must be the artist's own. A verified
    channel is not enough on its own either: verified businesses re-upload
    anything, so the title has to open with the artist's name, as a label
    upload does.
    """
    key = normalise(artist)
    if not key:
        return False
    channel = normalise(video.channel)
    title = normalise(video.title)
    if channel.endswith("topic"):
        channel = channel[: -len("topic")].strip()
        return key in channel or title.startswith(key)
    if key in channel:
        return True
    return video.verified and title.startswith(key)


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
                    _parse_length(renderer),
                    _is_verified(renderer),
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


def _is_verified(renderer: dict) -> bool:
    """The verified tick YouTube shows on official artist and label channels."""
    for badge in renderer.get("ownerBadges") or []:
        style = (badge.get("metadataBadgeRenderer") or {}).get("style") or ""
        if "VERIFIED" in style:
            return True
    return False


def _parse_length(renderer: dict) -> int | None:
    text = (renderer.get("lengthText") or {}).get("simpleText")
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


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


def build_page(candidates: Sequence[Candidate], title: str = "discolab checklist") -> str:
    sections = "\n".join(
        _section(index, candidate) for index, candidate in enumerate(candidates, start=1)
    )
    with_embed = sum(1 for candidate in candidates if candidate.videos)
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
  .alts {{ list-style: none; padding-left: 0; }}
  .alts a {{ font-size: 12px; }}
  .fallback img {{ width: 100%; max-width: 320px; border-radius: 4px; display: block; }}
  .fallback p {{ font-size: 13px; opacity: .75; margin: 6px 0; }}
  #bar {{ position: sticky; top: 0; background: Canvas; padding: 8px 0; z-index: 2;
         border-bottom: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }}
  #count {{ font-weight: 600; }}
  .dead h2 {{ opacity: .6; }}
</style>
<div id="bar">
  Kept <span id="count">0 of {len(candidates)}</span>.
  <span id="playable">{with_embed} embeds</span>.
  Criterion: fewer than 3 kept means the query is wrong, the data is too thin, or blank space is not fertile.
</div>
<h1>{html.escape(title)}</h1>
<p>One embed per artist, plus alternative official uploads and links to the other catalogues. Tick the ones worth playing again. Only tracks from the artist's own, distributor or verified channel are embedded, and a player that reports the uploader blocked embedding is swapped for a link.</p>
{sections}
<script>
  const boxes = [...document.querySelectorAll('input.keep')];
  const label = document.getElementById('count');
  const playable = document.getElementById('playable');
  let dead = 0;

  function update() {{
    const kept = boxes.filter(b => b.checked);
    label.textContent = kept.length + ' of ' + boxes.length;
    boxes.forEach(b => b.closest('section').classList.toggle('dead',
      boxes.some(o => o.checked) && !b.checked));
    playable.textContent = (boxes.length) + ' candidates, ' + dead + ' blocked by the uploader';
  }}
  boxes.forEach(b => b.addEventListener('change', update));

  function markDead(section) {{
    const player = section.querySelector('.player');
    const fallback = section.querySelector('.fallback');
    if (player) player.hidden = true;
    if (fallback) fallback.hidden = false;
    section.dataset.dead = '1';
    dead += 1;
    update();
  }}

  // The player API reports error 101 or 150 when the uploader disabled embedding,
  // which is otherwise invisible until a listener hits a blank frame.
  window.onYouTubeIframeAPIReady = function () {{
    document.querySelectorAll('iframe.yt').forEach(function (frame) {{
      const section = frame.closest('section');
      new YT.Player(frame, {{
        events: {{ onError: function () {{ markDead(section); }} }}
      }});
    }});
  }};
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
  update();
</script>
</html>
"""


def _section(index: int, candidate: Candidate) -> str:
    query = urllib.parse.quote(candidate.name)
    parts = [
        "<section>",
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

    primary = candidate.videos[0] if candidate.videos else None
    if primary is not None:
        parts.append(
            f'<div class="player"><iframe class="yt" src="{primary.embed_url}" '
            'loading="lazy" allow="accelerometer; clipboard-write; encrypted-media; '
            'picture-in-picture" allowfullscreen></iframe></div>'
        )
        parts.append(
            '<div class="fallback" hidden>'
            f'<a href="{primary.watch_url}">'
            f'<img src="{primary.thumbnail_url}" alt="" loading="lazy"></a>'
            "<p>This uploader blocked embedding. Play it on YouTube instead.</p>"
            "</div>"
        )
        parts.append(f'<p class="video-title">{html.escape(primary.label)}</p>')
    else:
        parts.append(
            '<p class="video-title">nothing resolved, use the YouTube search link above</p>'
        )

    alternatives = candidate.videos[1:]
    if alternatives:
        items = "".join(
            f'<li><a href="{video.watch_url}">{html.escape(video.label)}</a></li>'
            for video in alternatives
        )
        parts.append(f'<ul class="alts">Other uploads: {items}</ul>')

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

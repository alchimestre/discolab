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
from dataclasses import asdict, dataclass
from pathlib import Path

from .resolve import normalise

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SEARCH_TIMEOUT = 20.0
MAX_VIDEOS_PER_ARTIST = 10

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
    owned_titles: tuple[str, ...] = ()


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


def resolve_artist(
    name: str,
    owned_titles: Sequence[str] = (),
    limit: int = MAX_VIDEOS_PER_ARTIST,
) -> list[Video]:
    """Official tracks for an artist, best first.

    Searches the artist name, then the titles of tracks already in the library,
    because a name alone returns a thin and repetitive pool and the owned
    titles find the exact uploads.

    Ordered by how much the upload can be trusted, then by whether it is a
    track the library already holds, so that when an uploader blocks embedding
    the page moves to the next best one.

    An artist with no official upload yields nothing, which is better than
    embedding a fan re-upload.
    """
    queries = [f"{name} - Topic", name]
    queries.extend(f"{name} {title}" for title in owned_titles[:3])
    owned_keys = [key for key in (normalise(title) for title in owned_titles) if key]

    found: dict[str, tuple[int, int, int, Video]] = {}
    order = 0
    for query in queries:
        for video in search_videos(query):
            if video.video_id in found:
                continue
            order += 1
            rank = official_rank(video, name)
            if rank is None or not is_track(video):
                continue
            title_key = normalise(video.title)
            # A track the library already holds proves nothing when the point is
            # to test whether the graph surfaces something new, so it ranks last.
            fresh = 1 if any(key in title_key for key in owned_keys) else 0
            found[video.video_id] = (rank, fresh, order, video)

    ranked = sorted(found.values(), key=lambda item: (item[0], item[1], item[2]))
    # An artist with their own channel or distributor channel does not need
    # uploads that only look official, which is where loose matches creep in.
    if ranked and ranked[0][0] <= 1:
        ranked = [item for item in ranked if item[0] <= 1]
    return [video for _, _, _, video in ranked[:limit]]


def is_track(video: Video) -> bool:
    """True for a single track rather than a set, mix or full album upload."""
    if SET_PATTERNS.search(video.title):
        return False
    if video.duration_seconds is not None and video.duration_seconds > MAX_TRACK_SECONDS:
        return False
    return True


def official_rank(video: Video, artist: str) -> int | None:
    """How official an upload is, lower being better, or None to reject it.

    Fan re-uploads and aggregator channels are the common case in a name
    search, so an unverified channel has to be the artist's own. A verified
    channel is not enough either: verified businesses re-upload anything, so a
    title that merely opens with the artist's name is ranked last. That is how
    a Mexican song also called El Buho outranked the artist himself.
    """
    key = normalise(artist)
    if not key:
        return None
    channel = normalise(video.channel)
    title = normalise(video.title)
    if channel.endswith("topic"):
        stem = channel[: -len("topic")].strip()
        if key in stem:
            return 1
        return 2 if title.startswith(key) else None
    if key in channel:
        return 0
    if video.verified and title.startswith(key):
        return 3
    return None


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


# A short title matches too much of the library by accident, so only a
# reasonably long one counts as proof of ownership by a different artist.
MIN_LIBRARY_TITLE_MATCH = 12


def library_keys(library_titles: Sequence[str]) -> list[str]:
    """Normalised library titles long enough to be proof rather than coincidence."""
    return [
        key
        for key in (normalise(title) for title in library_titles)
        if len(key) >= MIN_LIBRARY_TITLE_MATCH
    ]


def holds_track(
    video: Video,
    artist_titles: Sequence[str],
    keys: Sequence[str],
) -> bool:
    """True when the library already holds the track this video plays.

    Checks the artist's own titles first, then the whole library, because a
    track can be held under a collaboration, a cover or a different spelling
    of the same artist.
    """
    title_key = normalise(video.title)
    if not title_key:
        return False
    if any(key and key in title_key for key in (normalise(t) for t in artist_titles)):
        return True
    return any(key in title_key for key in keys)


def ordered_videos(candidate: Candidate, keys: Sequence[str] = ()) -> list[Video]:
    """Fresh tracks first.

    Ordered here rather than at search time so that a cached resolution still
    puts something new in front of the listener.
    """
    fresh = [
        v for v in candidate.videos if not holds_track(v, candidate.owned_titles, keys)
    ]
    held = [
        v for v in candidate.videos if holds_track(v, candidate.owned_titles, keys)
    ]
    return fresh + held


def build_page(
    candidates: Sequence[Candidate],
    library_titles: Sequence[str] = (),
    title: str = "discolab checklist",
) -> str:
    keys = library_keys(library_titles)
    sections = "\n".join(
        _section(index, candidate, keys)
        for index, candidate in enumerate(candidates, start=1)
    )
    with_embed = sum(1 for candidate in candidates if candidate.videos)
    already_held = sum(
        1
        for candidate in candidates
        if candidate.videos
        and holds_track(
            ordered_videos(candidate, keys)[0], candidate.owned_titles, keys
        )
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
  .tag {{ display: inline-block; font-size: 11px; padding: 0 5px; border-radius: 8px;
         border: 1px solid currentColor; opacity: .8; }}
  .tag.already-held {{ color: var(--warn, #b45309); }}
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
  <span id="blocked">no embeds blocked</span>.
  {already_held} of {with_embed} embeds are tracks you already hold, matched by title, so ticking those tests recognition rather than discovery.
  Criterion: fewer than 3 new tracks kept means the query is wrong, the data is too thin, or blank space is not fertile.
</div>
<h1>{html.escape(title)}</h1>
<p>One embed per artist, plus alternative official uploads and links to the other catalogues. Tick the ones worth playing again. Only tracks from the artist's own, distributor or verified channel are embedded. If an uploader blocked embedding the page moves to the next official upload, and only falls back to a link when none of them play.</p>
{sections}
<script>
  const boxes = [...document.querySelectorAll('input.keep')];
  const label = document.getElementById('count');
  const blocked = document.getElementById('blocked');
  let dead = 0;

  function update() {{
    const kept = boxes.filter(b => b.checked);
    label.textContent = kept.length + ' of ' + boxes.length;
    boxes.forEach(b => b.closest('section').classList.toggle('dead',
      boxes.some(o => o.checked) && !b.checked));
    blocked.textContent = dead
      ? dead + ' of ' + boxes.length + ' blocked by the uploader'
      : 'no embeds blocked';
  }}
  boxes.forEach(b => b.addEventListener('change', update));

  function markDead(section) {{
    const player = section.querySelector('.player');
    const fallback = section.querySelector('.fallback');
    if (player) player.hidden = true;
    const note = fallback && fallback.querySelector('.fallback-note');
    if (note) {{
      const tried = section.dataset.attempts || '0';
      const pool = JSON.parse(section.dataset.videos || '[]').length;
      note.textContent = 'All ' + tried + ' of ' + pool
        + ' official uploads block embedding. Play it on YouTube instead.';
    }}
    if (fallback) fallback.hidden = false;
    section.dataset.dead = '1';
    dead += 1;
    update();
  }}

  function setLabel(section, video) {{
    if (!video) return;
    const line = section.querySelector('.video-title .playing');
    if (line) line.textContent = video.label;
    const tag = section.querySelector('.video-title .tag');
    if (tag) {{
      tag.textContent = video.held ? 'already held' : 'new';
      tag.className = 'tag ' + (video.held ? 'already-held' : 'new');
    }}
  }}

  // The player API reports error 101 or 150 when an uploader disabled embedding.
  // Most artists have several official uploads, so move to the next one rather
  // than sending the listener elsewhere; only give up when none of them play.
  window.onYouTubeIframeAPIReady = function () {{
    document.querySelectorAll('iframe.yt').forEach(function (frame) {{
      const section = frame.closest('section');
      const videos = JSON.parse(section.dataset.videos || '[]');
      let index = 0;
      let attempts = 0;
      let player;
      player = new YT.Player(frame, {{
        events: {{
          onError: function () {{
            attempts += 1;
            section.dataset.attempts = attempts;
            index += 1;
            if (index < videos.length) {{
              player.loadVideoById(videos[index].id);
              setLabel(section, videos[index]);
            }} else {{
              markDead(section);
            }}
          }}
        }}
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


def _section(index: int, candidate: Candidate, keys: Sequence[str]) -> str:
    query = urllib.parse.quote(candidate.name)
    videos = ordered_videos(candidate, keys)
    playlist = [
        {"id": v.video_id, "label": v.label, "held": holds_track(v, candidate.owned_titles, keys)}
        for v in videos
    ]
    data_videos = html.escape(json.dumps(playlist, ensure_ascii=False), quote=True)
    parts = [
        f'<section data-videos="{data_videos}">',
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

    primary = videos[0] if videos else None
    if primary is not None:
        held = holds_track(primary, candidate.owned_titles, keys)
        tag = "already held" if held else "new"
        parts.append(
            f'<div class="player"><iframe class="yt" src="{primary.embed_url}" '
            'loading="lazy" allow="accelerometer; clipboard-write; encrypted-media; '
            'picture-in-picture" allowfullscreen></iframe></div>'
        )
        parts.append(
            '<div class="fallback" hidden>'
            f'<a href="{primary.watch_url}">'
            f'<img src="{primary.thumbnail_url}" alt="" loading="lazy"></a>'
            "<p class=\"fallback-note\">This uploader blocks embedding.</p>"
            "</div>"
        )
        parts.append(
            f'<p class="video-title"><span class="tag {tag.replace(" ", "-")}">{tag}</span> '
            f'<span class="playing">{html.escape(primary.label)}</span></p>'
        )
    else:
        parts.append(
            '<p class="video-title">nothing resolved, use the YouTube search link above</p>'
        )

    alternatives = videos[1:]
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


def load_video_cache(path: Path) -> dict[str, list[Video]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        name: [Video(**item) for item in items]
        for name, items in data.items()
        if isinstance(items, list)
    }


def save_video_cache(path: Path, videos: dict[str, list[Video]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        name: [asdict(video) for video in items] for name, items in videos.items()
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


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

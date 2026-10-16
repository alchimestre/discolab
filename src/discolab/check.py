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
# The pool stays large so a blocked upload has somewhere to go; the list shown
# is short, or it crowds out the text beside it.
MAX_ALTERNATIVES_SHOWN = 3

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
    known_titles: tuple[tuple[str, int], ...] = ()
    seeds: tuple[str, ...] = ()
    external: bool = False
    source_kind: str = "bridge"
    seed_count: int = 0
    areas: int = 0
    styles: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


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
    queries = [f"{name} - Topic", f"{name} official audio", name]
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


# A channel that is the artist plus one of these is still the artist's channel.
CHANNEL_SUFFIXES = frozenset(
    {
        "topic", "official", "officialaudio", "officialvideo", "officialartist",
        "vevo", "music", "musica", "records", "recordings", "band", "dj", "tv",
        "hq", "musicvideo", "label", "sound", "audio",
    }
)


def _channel_is_artist(channel_key: str, artist_key: str) -> bool:
    """True when the channel is the artist, not merely a name containing them.

    Containment alone let Carrier Europe, a vehicle company, pass as the
    musician Carrier. The channel has to be the name, optionally followed by a
    word such as Official or Topic.
    """
    if not channel_key or not artist_key or not channel_key.startswith(artist_key):
        return False
    rest = channel_key[len(artist_key) :].strip()
    return not rest or rest.replace(" ", "") in CHANNEL_SUFFIXES


def official_rank(video: Video, artist: str) -> int | None:
    """How official an upload is, lower being better, or None to reject it.

    Fan re-uploads and aggregator channels are the common case in a name
    search, so an unverified channel has to be the artist's own. A verified
    channel is not enough either: verified businesses re-upload anything, so
    the title has to read as "artist - track" rather than merely open with the
    artist's name, which is how "Carrier Montluel" passed as Carrier.
    """
    key = normalise(artist)
    if not key:
        return None
    channel = normalise(video.channel)
    title = normalise(video.title)
    if channel.endswith("topic"):
        stem = channel[: -len("topic")].strip()
        if _channel_is_artist(stem, key):
            return 1
        return 2 if title.startswith(key) else None
    if _channel_is_artist(channel, key):
        return 0
    if video.verified and re.match(
        rf"^\s*{re.escape(artist)}\s*[-–—:|·(]", video.title, re.IGNORECASE
    ):
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
    known_titles: Sequence[tuple[str, int]],
    keys: Sequence[str],
) -> bool:
    """True when the library physically holds the track this video plays.

    Checks the artist's own titles first, then the whole library, because a
    track can be held under a collaboration, a cover or a different spelling
    of the same artist.
    """
    title_key = normalise(video.title)
    if not title_key:
        return False
    if any(key and key in title_key for key in (normalise(t) for t, _ in known_titles)):
        return True
    return any(key in title_key for key in keys)


def track_plays(video: Video, known_titles: Sequence[tuple[str, int]]) -> int:
    """How often history shows this track played, 0 when it never was."""
    title_key = normalise(video.title)
    if not title_key:
        return 0
    best = 0
    for title, plays in known_titles:
        key = normalise(title)
        if key and key in title_key:
            best = max(best, plays)
    return best


def tag_for(
    video: Video,
    known_titles: Sequence[tuple[str, int]],
    keys: Sequence[str],
) -> tuple[str, str]:
    """The label and css class for an embed: held, played before, or new."""
    if holds_track(video, known_titles, keys):
        return "already held", "already-held"
    plays = track_plays(video, known_titles)
    if plays:
        return f"played {plays}x", "played"
    return "new", "new"


def ordered_videos(candidate: Candidate, keys: Sequence[str] = ()) -> list[Video]:
    """Fresh tracks first.

    Ordered here rather than at search time so that a cached resolution still
    puts something new in front of the listener.
    """
    def rank(video: Video) -> tuple[bool, bool]:
        return (
            holds_track(video, candidate.known_titles, keys),
            track_plays(video, candidate.known_titles) > 0,
        )

    return sorted(candidate.videos, key=rank)


def build_page(
    candidates: Sequence[Candidate],
    library_titles: Sequence[str] = (),
    votes: dict[str, dict] | None = None,
    title: str = "discolab checklist",
    nav: str = "",
) -> str:
    keys = library_keys(library_titles)
    saved = votes or {}
    sections = "\n".join(
        _section(index, candidate, keys, saved.get(candidate.name, {}))
        for index, candidate in enumerate(candidates, start=1)
    )
    with_embed = sum(1 for candidate in candidates if candidate.videos)
    already_held = sum(
        1
        for candidate in candidates
        if candidate.videos
        and holds_track(
            ordered_videos(candidate, keys)[0], candidate.known_titles, keys
        )
    )
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 1080px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 17px; margin: 0 0 4px; }}
  h2 small {{ font-weight: 400; opacity: .65; font-size: 13px; }}
  ul {{ margin: 6px 0 0; padding-left: 20px; }}
  li {{ font-size: 13px; opacity: .85; }}
  section {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 20px;
             align-items: start; padding: 16px 0;
             border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }}
  .media {{ position: sticky; top: 64px; }}
  .media .keep {{ margin-top: 8px; }}
  iframe {{ width: 100%; aspect-ratio: 16/9; border: 0; border-radius: 4px; }}
  @media (max-width: 760px) {{
    section {{ grid-template-columns: 1fr; }}
    .media {{ position: static; }}
  }}
  .links a {{ font-size: 13px; margin-right: 10px; }}
  .video-title {{ font-size: 13px; opacity: .7; margin: 4px 0 0; }}
  .tag {{ display: inline-block; font-size: 11px; padding: 0 5px; border-radius: 8px;
         border: 1px solid currentColor; opacity: .8; }}
  .tag.already-held {{ color: var(--warn, #b45309); }}
  .tag.played {{ opacity: .6; }}
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
{nav}
<p>One embed per artist, plus alternative official uploads and links to the other catalogues. Tick the ones worth playing again. Only tracks from the artist's own, distributor or verified channel are embedded. If an uploader blocked embedding the page moves to the next official upload, and only falls back to a link when none of them play.</p>
{sections}
<script>
  const boxes = [...document.querySelectorAll('input.keep')];
  const label = document.getElementById('count');
  const blocked = document.getElementById('blocked');
  let dead = 0;

  function sectionName(section) {{
    const heading = section.querySelector('h2').textContent.trim();
    return heading.replace(/^\\d+\\.\\s*/, '').split(' island')[0].trim();
  }}

  function save() {{
    const data = {{}};
    document.querySelectorAll('section').forEach(function (s) {{
      const tag = s.querySelector('.tag');
      data[sectionName(s)] = {{
        kept: s.querySelector('input.keep').checked,
        state: tag ? tag.textContent : '',
      }};
    }});
    fetch('/votes', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(data),
    }}).catch(function () {{}});
  }}

  // Ticks live on the server, so a reload or a rebuild brings them back.
  function restore() {{
    fetch('/votes').then(function (r) {{ return r.ok ? r.json() : {{}}; }})
      .then(function (data) {{
        document.querySelectorAll('section').forEach(function (s) {{
          const saved = data[sectionName(s)];
          if (saved) s.querySelector('input.keep').checked = !!saved.kept;
        }});
        render();
      }}).catch(function () {{}});
  }}

  function render() {{
    const kept = boxes.filter(b => b.checked);
    label.textContent = kept.length + ' of ' + boxes.length;
    boxes.forEach(b => b.closest('section').classList.toggle('dead',
      boxes.some(o => o.checked) && !b.checked));
    blocked.textContent = dead
      ? dead + ' of ' + boxes.length + ' blocked by the uploader'
      : 'no embeds blocked';
  }}

  function update() {{
    render();
    save();
  }}
  boxes.forEach(b => b.addEventListener('change', update));
  // Render only, so loading the page does not overwrite saved ticks with blanks.
  render();
  restore();

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
      tag.textContent = video.state;
      tag.className = 'tag ' + video.cls;
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
</script>
</html>
"""


def _provenance(candidate: Candidate) -> str:
    """Say where the artist came from, since the two sources mean different things."""
    if candidate.source_kind == "labels":
        via = ", ".join(candidate.seeds[:3]) or "unknown label"
        header = (
            f"not in your library · on {candidate.seed_count} of your labels "
            f"· {html.escape(via)}"
        )
    elif candidate.external:
        via = ", ".join(candidate.seeds[:3]) or "unknown seed"
        header = (
            f"not in your library · {candidate.seed_count} seed(s) across "
            f"{candidate.areas} area(s) · via {html.escape(via)}"
        )
    else:
        header = (
            f"island {candidate.component} of {candidate.component_size} artists, "
            f"degree {candidate.degree}, bridge {candidate.bridge_score:.5f}"
        )
    if candidate.styles:
        header += f" · {html.escape(', '.join(candidate.styles[:4]))}"
    if candidate.labels:
        header += f" · on {html.escape(', '.join(candidate.labels[:2]))}"
    return header


def _section(
    index: int, candidate: Candidate, keys: Sequence[str], vote: dict
) -> str:
    query = urllib.parse.quote(candidate.name)
    videos = ordered_videos(candidate, keys)
    playlist = []
    for video in videos:
        label, css = tag_for(video, candidate.known_titles, keys)
        playlist.append(
            {"id": video.video_id, "label": video.label, "state": label, "cls": css}
        )
    data_videos = html.escape(json.dumps(playlist, ensure_ascii=False), quote=True)

    info = [
        '<div class="info">',
        f"<h2>{index}. {html.escape(candidate.name)} <small>{_provenance(candidate)}</small></h2>",
        '<p class="links">'
        f'<a href="https://open.spotify.com/search/{query}">Spotify</a>'
        f'<a href="https://www.youtube.com/results?search_query={query}">YouTube</a>'
        f'<a href="https://bandcamp.com/search?q={query}">Bandcamp</a>'
        f'<a href="https://www.discogs.com/search/?q={query}&type=artist">Discogs</a>'
        f'<a href="https://musicbrainz.org/search?query={query}&type=artist">MusicBrainz</a>'
        "</p>",
    ]

    alternatives = videos[1:1 + MAX_ALTERNATIVES_SHOWN]
    if alternatives:
        items = "".join(
            f'<li><a href="{video.watch_url}">{html.escape(video.label)}</a></li>'
            for video in alternatives
        )
        info.append(f'<ul class="alts">Other uploads: {items}</ul>')

    if candidate.tracks:
        items = "".join(
            f"<li>{html.escape(track['title'])} &middot; {html.escape(track['path'])}</li>"
            for track in candidate.tracks
        )
        info.append(f"<ul>{items}</ul>")
    info.append("</div>")

    media = ['<div class="media">']
    primary = videos[0] if videos else None
    if primary is not None:
        tag, css = tag_for(primary, candidate.known_titles, keys)
        media.append(
            f'<div class="player"><iframe class="yt" src="{primary.embed_url}" '
            'loading="lazy" allow="accelerometer; clipboard-write; encrypted-media; '
            'picture-in-picture" allowfullscreen></iframe></div>'
        )
        media.append(
            '<div class="fallback" hidden>'
            f'<a href="{primary.watch_url}">'
            f'<img src="{primary.thumbnail_url}" alt="" loading="lazy"></a>'
            "<p class=\"fallback-note\">This uploader blocks embedding.</p>"
            "</div>"
        )
        media.append(
            f'<p class="video-title"><span class="tag {css}">{tag}</span> '
            f'<span class="playing">{html.escape(primary.label)}</span></p>'
        )
    else:
        media.append(
            '<p class="video-title">nothing resolved, use the YouTube link on the left</p>'
        )
    checked = " checked" if vote.get("kept") else ""
    media.append(
        f'<label class="keep"><input type="checkbox" class="keep"{checked}> keep</label>'
    )
    media.append("</div>")

    return "\n".join(
        [f'<section data-videos="{data_videos}">'] + info + media + ["</section>"]
    )


# Bump this whenever resolve_artist's rules change. Without it a cached page
# keeps serving videos the current rules would reject: the substring match
# that let CANAL+ Sport Afrique rank as the band Can lived on in the cache
# long after the rule was fixed.
RESOLVER_VERSION = 2


def load_video_cache(path: Path) -> dict[str, list[Video]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or data.get("version") != RESOLVER_VERSION:
        return {}
    entries = data.get("videos")
    if not isinstance(entries, dict):
        return {}
    return {
        name: [Video(**item) for item in items]
        for name, items in entries.items()
        if isinstance(items, list)
    }


def save_video_cache(path: Path, videos: dict[str, list[Video]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": RESOLVER_VERSION,
        "videos": {
            name: [asdict(video) for video in items] for name, items in videos.items()
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def write_page(content: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class _VoteHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        votes_path = getattr(self.server, "votes_path", None)
        if self.path.rstrip("/") == "/votes" and votes_path is not None:
            body = json.dumps(read_votes(votes_path), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:
        votes_path = getattr(self.server, "votes_path", None)
        if self.path.rstrip("/") != "/votes" or votes_path is None:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400)
            return
        votes_path.parent.mkdir(parents=True, exist_ok=True)
        votes_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.send_response(204)
        self.end_headers()


def read_votes(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_cluster_index(
    clusters: Sequence[dict], built: Sequence[int], title: str = "discolab clusters"
) -> str:
    """An index of the taste clusters, linking to the pages that exist."""
    ready = set(built)
    rows = []
    for cluster in clusters:
        styles = ", ".join(cluster["styles"][:4]) or "no styles yet"
        members = ", ".join(cluster["members"][:5])
        link = (
            f'<a href="cluster-{cluster["cluster"]}.html">{cluster["size"]} artists</a>'
            if cluster["cluster"] in ready
            else f'{cluster["size"]} artists'
        )
        rows.append(
            "<tr>"
            f'<td>{cluster["cluster"]}</td><td>{link}</td>'
            f"<td>{html.escape(styles)}</td><td>{html.escape(members)}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 1080px; margin: 0 auto; padding: 24px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 6px 10px 6px 0; vertical-align: top;
            border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }}
  th {{ font-size: 13px; opacity: .7; }}
  td:nth-child(4) {{ font-size: 13px; opacity: .8; }}
</style>
<h1>{html.escape(title)}</h1>
<p>A cluster is a group of artists that co-occur in your own listening. Pick one to see what it
leads to, or open its periphery to see the members that barely connect to it.</p>
<table>
<tr><th>cluster</th><th>frontier</th><th>usual styles</th><th>biggest members</th></tr>
{''.join(rows)}
</table>
</html>
"""


def serve_directory(
    directory: Path,
    filename: str,
    votes_path: Path | None = None,
    host: str = "127.0.0.1",
) -> tuple[str, http.server.ThreadingHTTPServer]:
    """Serve the page over http, not file.

    YouTube returns error 153 for embeds loaded from file:// because the
    request carries no referrer.
    """
    server = http.server.ThreadingHTTPServer(
        (host, 0), functools.partial(_VoteHandler, directory=str(directory))
    )
    server.votes_path = votes_path
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{host}:{server.server_port}/{filename}", server

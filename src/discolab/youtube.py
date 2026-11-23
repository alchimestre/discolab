"""Find an artist's official uploads on YouTube, and remember them."""

from __future__ import annotations

import json
import re
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
            fresh = 1 if any(_contains_title(title_key, key) for key in owned_keys) else 0
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


def _contains_title(video_key: str, track_key: str) -> bool:
    if not track_key:
        return False
    start = video_key.find(track_key)
    while start != -1:
        end = start + len(track_key)
        starts_word = start == 0 or not video_key[start - 1].isalnum()
        ends_word = end == len(video_key) or not video_key[end].isalnum()
        if starts_word and ends_word:
            return True
        start = video_key.find(track_key, start + 1)
    return False


# Bump this whenever resolve_artist's rules change. Without it a cached page
# keeps serving videos the current rules would reject: the substring match
# that let CANAL+ Sport Afrique rank as the band Can lived on in the cache
# long after the rule was fixed.
RESOLVER_VERSION = 3


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

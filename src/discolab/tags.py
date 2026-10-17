"""Last.fm's community tags, the graph's only genre source.

Genre coverage comes from listeners rather than a catalogue, which is why it says
"hardgroove", "acidcore", "industrial techno" instead of a label a store would
file under. Tags cost a request each, unlike the similar-artist call that returns
twenty artists at once, so this is a paced walk rather than a side effect of
expansion.

Last.fm's top tags mix genres with places, platforms and moods, so the pile is
filtered before it reaches the graph.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
RETRIES = 3
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# A tag has to be more than a scrap: below a tenth of the top tag it is usually
# one listener's word, and a floor of eight keeps out the tail entirely.
MIN_COUNT = 8
SHARE = 0.12
KEEP = 3

# Last.fm tags mix genres with where an artist is from and what mood they are
# for. Those name an address rather than a sound, and they fold into no family,
# so they would only thicken the map's "Other" without saying anything.
NON_GENRE = frozenset(
    {
        "australia", "australian", "austria", "austrian", "belgian", "belgium",
        "brazil", "brazilian", "british", "canada", "canadian", "denmark",
        "danish", "dutch", "english", "finland", "finnish", "france", "french",
        "german", "germany", "ireland", "irish", "italian", "italy", "japan",
        "japanese", "korea", "korean", "mexican", "mexico", "netherlands",
        "norway", "norwegian", "polish", "poland", "portugal", "portuguese",
        "russia", "russian", "spain", "spanish", "sweden", "swedish",
        "swiss", "switzerland", "turkey", "turkish", "uk", "united kingdom",
        "united states", "usa", "american",
        "afghan", "algerian", "andean", "arabic", "balearic", "balkan",
        "caribbean", "egyptian", "ethiopian", "ghanaian", "indian", "iranian",
        "iraqi", "israeli", "latin", "lebanese", "malian", "moroccan", "nepali",
        "nigerian", "nordic", "pakistani", "palestinian", "persian", "sahelian",
        "syrian", "tibetan", "tunisian",
        "bandcamp", "soundcloud", "spotify", "yandex music", "youtube",
        "seen live", "favorites", "favourite", "favourites", "loved",
        "under 2000 listeners", "female vocalists", "male vocalists",
        "chill", "meditation", "relaxing", "sleep", "study", "instrumental",
        "50s", "60s", "70s", "80s", "90s", "00s", "10s",
        "egypt", "lebanon", "mali", "morocco", "cambodia", "cambodian",
        "greek", "hindi", "syria", "iraq", "iran", "algeria", "tunisia",
    }
)

# Demonyms are endless and Last.fm's tail is full of them. They almost all end
# the same two ways, which catches the ones no list would hold.
NON_GENRE_SUFFIXES = ("ese", "ish")


def _is_genre(name: str) -> bool:
    folded = name.casefold()
    return folded not in NON_GENRE and not folded.endswith(NON_GENRE_SUFFIXES)


class TagError(RuntimeError):
    pass


class LastfmTagger:
    """Artist tags from Last.fm, paced to stay under its published ceiling."""

    source = "lastfm-tags"

    def __init__(self, api_key: str, rate: float = 4.0):
        self.api_key = api_key
        self.interval = 1.0 / rate if rate > 0 else 0.0

    def _call(self, method: str, artist: str) -> dict:
        params = {
            "method": method,
            "artist": artist,
            "api_key": self.api_key,
            "format": "json",
            "autocorrect": "1",
        }
        request = urllib.request.Request(
            API_ROOT + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "discolab/0.1"},
        )
        for attempt in range(RETRIES):
            time.sleep(self.interval)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as error:
                if error.code in RETRY_STATUS and attempt < RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise TagError(f"{artist}: HTTP {error.code}") from error
            except Exception as error:
                if attempt < RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise TagError(f"{artist}: {error}") from error
        return {}

    def tags(self, artist: str) -> list[str]:
        """The genres this artist is tagged with, strongest first."""
        payload = self._call("artist.gettoptags", artist)
        if "error" in payload:
            # An artist Last.fm does not know is an answer, not a failure.
            return []
        scored: list[tuple[int, str]] = []
        for item in payload.get("toptags", {}).get("tag", []) or []:
            name = (item.get("name") or "").strip()
            if not name or not _is_genre(name):
                continue
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                scored.append((count, name))
        if not scored:
            return []
        floor = max(MIN_COUNT, int(max(count for count, _ in scored) * SHARE))
        return [name for count, name in scored if count >= floor][:KEEP]

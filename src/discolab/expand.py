"""Leave the corpus.

Ranking artists that bridge your own graph only ever returns artists you
already have, because every node came from your own data. The bridge ranks are
seeds: each one says which part of your taste is worth expanding from. The
expansion itself has to arrive from outside, and this is where it does.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
REQUEST_INTERVAL = 0.25
RETRIES = 3
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ExpandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Suggestion:
    seed: str
    name: str
    score: float


class LastfmSimilarExpander:
    """One hop out, through Last.fm's artist similarity."""

    source = "lastfm-similar"

    def __init__(self, api_key: str, per_seed: int = 20):
        self.api_key = api_key
        self.per_seed = per_seed

    def tags(self, artist: str) -> list[str]:
        """Top tags, which is the only genre signal Last.fm exposes."""
        params = {
            "method": "artist.gettoptags",
            "artist": artist,
            "api_key": self.api_key,
            "format": "json",
            "autocorrect": 1,
        }
        url = API_ROOT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "discolab/0.1"})
        time.sleep(REQUEST_INTERVAL)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return []
        if "error" in payload:
            return []
        found = payload.get("toptags", {}).get("tag", [])
        return [
            item.get("name", "")
            for item in found
            if item.get("name") and float(item.get("count", 0) or 0) > 0
        ]

    def similar(self, artist: str) -> list[Suggestion]:
        params = {
            "method": "artist.getsimilar",
            "artist": artist,
            "api_key": self.api_key,
            "format": "json",
            "limit": self.per_seed,
        }
        url = API_ROOT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "discolab/0.1"})

        payload: dict | None = None
        for attempt in range(RETRIES):
            time.sleep(REQUEST_INTERVAL)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as error:
                if error.code in RETRY_STATUS and attempt < RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise ExpandError(f"{artist}: HTTP {error.code}") from error
            except Exception as error:
                if attempt < RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise ExpandError(f"{artist}: {error}") from error

        if payload is None:
            return []
        if "error" in payload:
            # An unknown artist is a normal answer here, not a failure.
            return []

        found = payload.get("similarartists", {}).get("artist", [])
        return [
            Suggestion(
                seed=artist,
                name=item.get("name", ""),
                score=float(item.get("match", 0.0) or 0.0),
            )
            for item in found
            if item.get("name")
        ]

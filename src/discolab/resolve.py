"""Turn raw provider output into canonical entities.

No filesystem and no graph access here, so the whole module is unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .model import Artist, Catalog, Container, Credit, RawTrack, Track

# Artist fields that name nobody.
JUNK_ARTISTS = frozenset(
    {"", "various artists", "various", "va", "v a", "unknown", "unknown artist"}
)

# Mix labels that are a version rather than a person.
GENERIC_MIX_WORDS = frozenset(
    {
        "original", "original mix", "original version", "original extended mix",
        "extended", "extended mix", "extended version", "club", "club mix",
        "radio", "radio edit", "instrumental", "vocal", "vocal mix", "dub",
        "main", "main mix", "album", "album version", "single", "single version",
        "bonus", "bonus track", "remaster", "remastered", "live", "acoustic",
        "version", "12", "12 inch", "7",
    }
)

GENERIC_MIX_HEADS = frozenset(
    {
        "original", "extended", "club", "radio", "instrumental", "vocal", "dub",
        "main", "album", "single", "bonus", "remaster", "remastered", "live",
        "acoustic", "version",
    }
)

# Dropped from the end of a credit, as in 'Trutopia extended remix'.
TRAILING_DESCRIPTORS = GENERIC_MIX_HEADS | frozenset(
    {"remix", "rmx", "rework", "bootleg", "refix", "flip", "mashup", "edit", "mix", "vip", "version"}
)

# Dropped from the start of a credit, as in 'feat Iris Gold'.
LEADING_QUALIFIERS = frozenset(
    {"feat", "ft", "featuring", "with", "prod", "produced", "by"}
)

_ARTIST_SEPARATORS = re.compile(
    r"\s*(?:;|\s+vs\.?\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+)\s*", re.IGNORECASE
)
_AMP_SEPARATOR = re.compile(r"\s+&\s+")
_CREDIT_SPLIT = re.compile(
    r"\s*(?:,|\s+&\s+|\s+x\s+|\s+vs\.?\s+|\s+-\s+)\s*", re.IGNORECASE
)
_FEATURE_CREDIT = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", re.IGNORECASE
)
_MIX_CREDIT = re.compile(
    r"[\(\[]\s*([^\)\]]*?)\s*(?:remix|rmx|rework|bootleg|refix|flip|mashup|edit|mix)\s*[\)\]]",
    re.IGNORECASE,
)


def normalise(value: str) -> str:
    """Comparison key for a name: accent-free, case-free, punctuation-free."""
    folded = unicodedata.normalize("NFKD", value)
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    cleaned = re.sub(r"[^0-9a-z]+", " ", stripped.casefold())
    key = " ".join(cleaned.split())
    if key:
        return key
    # A name written entirely in a non-Latin script folds to nothing, which
    # would collapse every such artist onto one empty key.
    return " ".join(value.casefold().split())


def is_junk_artist(value: str) -> bool:
    return normalise(value) in JUNK_ARTISTS


def is_generic_mix(value_key: str) -> bool:
    """True for version labels such as 'original mix' and bare years like '2022'."""
    if value_key in GENERIC_MIX_WORDS or value_key.isdigit():
        return True
    head = value_key.split(" ", 1)[0]
    return head in GENERIC_MIX_HEADS


def split_artists(raw: str) -> list[str]:
    """Split an artist field into individual names, dropping placeholder values."""
    candidates = _split_on(raw, _ARTIST_SEPARATORS)
    if len(candidates) == 1:
        # A comma only separates artists when it wraps a placeholder, as in
        # "Various Artists, Moving Still". Otherwise it belongs to the name,
        # as in "Earth, Wind & Fire".
        comma_parts = [part.strip() for part in raw.split(",") if part.strip()]
        if len(comma_parts) > 1 and any(is_junk_artist(part) for part in comma_parts):
            candidates = comma_parts
    if len(candidates) == 1 and "," not in raw:
        candidates = _split_on(raw, _AMP_SEPARATOR)
    names = [candidate for candidate in candidates if not is_junk_artist(candidate)]
    return [name for name in names if normalise(name)]


def parse_credits(primary_artist: str, title: str) -> list[tuple[str, str]]:
    """Return (name, role) pairs credited in a title, excluding the primary artist."""
    found: list[tuple[str, str]] = []
    for match in _FEATURE_CREDIT.finditer(title):
        found.extend((name, "feature") for name in _clean_credit(match.group(1)))
    for match in _MIX_CREDIT.finditer(title):
        found.extend((name, "remix") for name in _clean_credit(match.group(1)))

    primary_key = normalise(primary_artist)
    credits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, role in found:
        key = normalise(name)
        if not key or key == primary_key or is_generic_mix(key):
            continue
        if (key, role) in seen:
            continue
        seen.add((key, role))
        credits.append((name.strip(), role))
    return credits


def build_catalog(raw_tracks: Iterable[RawTrack]) -> Catalog:
    artist_names: dict[str, str] = {}
    track_records: dict[str, dict] = {}
    containers: dict[str, Container] = {}
    memberships: set[tuple[str, str]] = set()
    credits: dict[tuple[str, str, str], Credit] = {}
    skipped: list[tuple[str, str]] = []

    for raw in raw_tracks:
        names = split_artists(raw.artist_raw or "")
        if not names:
            skipped.append((raw.path, "no artist"))
            continue
        title_key = normalise(raw.title_raw) or raw.title_raw.casefold().strip()
        if not title_key:
            skipped.append((raw.path, "no title"))
            continue

        primary = names[0]
        artist_key = normalise(primary)
        # Keyed without duration so lossless and lossy copies of one track merge.
        track_key = f"{artist_key}::{title_key}"
        artist_names.setdefault(artist_key, primary)

        record = track_records.get(track_key)
        if record is None:
            record = {
                "key": track_key,
                "title": raw.title_raw.strip(),
                "artist_key": artist_key,
                "artist_name": primary,
                "duration_ms": raw.duration_ms,
                "paths": [],
                "plays": 0,
            }
            track_records[track_key] = record
        elif record["duration_ms"] is None:
            record["duration_ms"] = raw.duration_ms
        # A scrobble has no file, so it contributes plays and containers only.
        if raw.path:
            record["paths"].append(raw.path)
        record["plays"] += raw.plays

        for extra in names[1:]:
            _add_credit(credits, artist_names, extra, artist_key, track_key, "collab")
        for name, role in parse_credits(primary, raw.title_raw):
            _add_credit(credits, artist_names, name, artist_key, track_key, role)

        for kind, name in raw.containers:
            key = _container_key(kind, name)
            if key is None:
                continue
            containers.setdefault(key, Container(key=key, kind=kind, name=name.strip()))
            memberships.add((key, track_key))

    return Catalog(
        artists=tuple(Artist(key=key, name=name) for key, name in artist_names.items()),
        tracks=tuple(Track(**record) for record in track_records.values()),
        containers=tuple(containers.values()),
        memberships=tuple(sorted(memberships)),
        credits=tuple(credits.values()),
        skipped=tuple(skipped),
    )


def _split_on(value: str, pattern: re.Pattern[str]) -> list[str]:
    if not pattern.search(value):
        return [value.strip()]
    return [part.strip() for part in pattern.split(value) if part.strip()]


def _clean_credit(value: str) -> list[str]:
    """Split a credit string and strip version words, as in 'X extended remix'."""
    names: list[str] = []
    for part in _CREDIT_SPLIT.split(value):
        tokens = part.split()
        while len(tokens) > 1 and _is_descriptor(tokens[-1]):
            tokens.pop()
        while len(tokens) > 1 and _is_qualifier(tokens[0]):
            tokens.pop(0)
        name = " ".join(tokens).strip()
        if name:
            names.append(name)
    return names


def _is_descriptor(token: str) -> bool:
    return token.casefold().strip(".") in TRAILING_DESCRIPTORS


def _is_qualifier(token: str) -> bool:
    return token.casefold().strip(".") in LEADING_QUALIFIERS


def _add_credit(
    credits: dict[tuple[str, str, str], Credit],
    artist_names: dict[str, str],
    name: str,
    primary_key: str,
    track_key: str,
    role: str,
) -> None:
    key = normalise(name)
    if not key or key == primary_key or is_generic_mix(key):
        return
    artist_names.setdefault(key, name.strip())
    credits.setdefault(
        (key, track_key, role),
        Credit(artist_key=key, artist_name=name.strip(), track_key=track_key, role=role),
    )


def _container_key(kind: str, name: str) -> str | None:
    key = normalise(name)
    if not key:
        return None
    return f"{kind}::{key}"

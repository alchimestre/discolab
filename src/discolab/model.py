from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RawTrack:
    """One audio file as a provider read it, before resolution."""

    source: str
    path: str
    artist_raw: str | None
    title_raw: str
    album_raw: str | None
    duration_ms: int | None
    containers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Artist:
    key: str
    name: str


@dataclass(frozen=True, slots=True)
class Track:
    key: str
    title: str
    artist_key: str
    artist_name: str
    duration_ms: int | None
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Container:
    key: str
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class Credit:
    artist_key: str
    artist_name: str
    track_key: str
    role: str


@dataclass(frozen=True, slots=True)
class Catalog:
    artists: tuple[Artist, ...]
    tracks: tuple[Track, ...]
    containers: tuple[Container, ...]
    memberships: tuple[tuple[str, str], ...]
    credits: tuple[Credit, ...]
    skipped: tuple[tuple[str, str], ...]

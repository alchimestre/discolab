from __future__ import annotations

from typing import NamedTuple, Protocol

from ..model import RawTrack


class ScanResult(NamedTuple):
    source: str
    tracks: tuple[RawTrack, ...]
    skipped: tuple[tuple[str, str], ...]


class SeedProvider(Protocol):
    """A source of played tracks. One is required; more are additive."""

    name: str

    def scan(self) -> ScanResult: ...

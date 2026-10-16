from __future__ import annotations

from pathlib import Path

import mutagen

from ..model import RawTrack
from .base import ScanResult

AUDIO_SUFFIXES = frozenset(
    {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav"}
)


class LocalFilesProvider:
    """Reads audio files from disk. Tags and filenames only, no network."""

    name = "local_files"

    def __init__(self, roots: tuple[Path, ...]):
        self.roots = roots

    def scan(self) -> ScanResult:
        tracks: list[RawTrack] = []
        skipped: list[tuple[str, str]] = []
        for root in self.roots:
            if not root.is_dir():
                skipped.append((str(root), "not a directory"))
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.casefold() not in AUDIO_SUFFIXES:
                    continue
                raw = _read(path, root)
                if isinstance(raw, str):
                    skipped.append((str(path), raw))
                else:
                    tracks.append(raw)
        return ScanResult(source=self.name, tracks=tuple(tracks), skipped=tuple(skipped))


def _read(path: Path, root: Path) -> RawTrack | str:
    tags = _read_tags(path)
    filename_artist, filename_title = _split_filename(path.stem)

    artist_raw = filename_artist or tags.get("artist")
    title_raw = filename_title or tags.get("title") or path.stem
    album_raw = tags.get("album")

    containers: list[tuple[str, str]] = []
    if album_raw:
        containers.append(("album", album_raw))
    relative = path.parent.relative_to(root)
    if relative.parts:
        containers.append(("folder", str(relative)))

    return RawTrack(
        source="local_files",
        path=str(path),
        artist_raw=artist_raw,
        title_raw=title_raw,
        album_raw=album_raw,
        duration_ms=tags.get("duration_ms"),
        containers=tuple(containers),
    )


def _split_filename(stem: str) -> tuple[str | None, str]:
    """Split 'Artist - Title' as DJ libraries name files."""
    head, separator, tail = stem.partition(" - ")
    if not separator or not head.strip() or not tail.strip():
        return None, stem.strip()
    return head.strip(), tail.strip()


def _read_tags(path: Path) -> dict:
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}

    result: dict = {}
    tags = audio.tags or {}
    for field in ("artist", "albumartist", "title", "album", "date", "year"):
        value = tags.get(field)
        if not value:
            continue
        text = value[0] if isinstance(value, list) else value
        text = str(text).strip()
        if text:
            result.setdefault(field, text)

    length = getattr(getattr(audio, "info", None), "length", None)
    if isinstance(length, (int, float)) and length > 0:
        result["duration_ms"] = int(length * 1000)
    return result

"""Dependency-free domain models shared by playback, UI, and persistence.

This module intentionally imports only the standard library.  It is introduced
first as an isolated W1 boundary; existing runtime callers remain unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

TrackSource = Literal["voice", "browse", "playlist", "recent", "queue"]
SelectionSource = Literal["voice", "browse", "playlist"]


class DomainError(ValueError):
    """Raised when a domain value violates its invariant."""


def normalize_youtube_url(value: str) -> tuple[str, str]:
    """Return ``(video_id, canonical_watch_url)`` for common YouTube forms."""
    text = str(value or "").strip()
    if not text:
        raise DomainError("track URL is required")
    match = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})", text)
    if not match:
        raise DomainError(f"not a supported YouTube URL: {value!r}")
    video_id = match.group(1)
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def _bounded_text(value: str, field: str, limit: int = 512) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError(f"{field} is required")
    if len(text) > limit:
        raise DomainError(f"{field} is too long")
    return text


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    canonical_url: str
    duration: Optional[float] = None
    source: TrackSource = "queue"
    artist: Optional[str] = None

    def __post_init__(self) -> None:
        video_id = _bounded_text(self.video_id, "video_id", 128)
        title = _bounded_text(self.title, "title")
        canonical_id, canonical_url = normalize_youtube_url(self.canonical_url)
        if video_id != canonical_id:
            raise DomainError("video_id does not match canonical_url")
        if self.source not in {"voice", "browse", "playlist", "recent", "queue"}:
            raise DomainError("unknown track source")
        if self.duration is not None and (not isinstance(self.duration, (int, float)) or self.duration <= 0):
            raise DomainError("duration must be positive")
        if self.artist is not None:
            _bounded_text(self.artist, "artist", 256)
        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "canonical_url", canonical_url)


class PlaybackState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    ENDED = "ended"


@dataclass(frozen=True)
class PlaybackRequest:
    tracks: tuple[Track, ...]
    start_index: int = 0
    source: TrackSource = "queue"

    def __post_init__(self) -> None:
        if not self.tracks or not all(isinstance(track, Track) for track in self.tracks):
            raise DomainError("tracks must contain Track values")
        if not isinstance(self.start_index, int) or not 0 <= self.start_index < len(self.tracks):
            raise DomainError("start_index is out of range")
        if self.source not in {"voice", "browse", "playlist", "recent", "queue"}:
            raise DomainError("unknown request source")


@dataclass(frozen=True)
class PlaybackEvent:
    generation: int
    request_id: int
    state: PlaybackState
    current_index: Optional[int] = None
    current_url: Optional[str] = None
    position: float = 0.0
    duration: Optional[float] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.generation < 0 or self.request_id < 0:
            raise DomainError("event IDs must be non-negative")
        if self.current_index is not None and self.current_index < 0:
            raise DomainError("current_index must be non-negative or None")
        if not isinstance(self.state, PlaybackState):
            raise DomainError("state must be PlaybackState")
        if not isinstance(self.position, (int, float)) or self.position < 0:
            raise DomainError("position must be non-negative")
        if self.duration is not None and (not isinstance(self.duration, (int, float)) or self.duration <= 0):
            raise DomainError("duration must be positive or None")
        if self.error is not None:
            _bounded_text(self.error, "error", 512)


@dataclass(frozen=True)
class TrackView:
    track: Track
    selected: bool = False
    current: bool = False
    resolving: bool = False
    failed: bool = False


@dataclass(frozen=True)
class UiSnapshot:
    playback: PlaybackState = PlaybackState.IDLE
    queue: tuple[TrackView, ...] = ()
    current_index: Optional[int] = None
    selected_source: Optional[SelectionSource] = None
    selected_index: Optional[int] = None
    voice_results: tuple[TrackView, ...] = ()
    browse_results: tuple[TrackView, ...] = ()
    playlist_results: tuple[TrackView, ...] = ()
    volume: int = 50
    backend: str = ""
    mic_level: float = 0.0
    error: Optional[str] = None
    busy: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.playback, PlaybackState):
            raise DomainError("playback must be PlaybackState")
        for name in ("current_index", "selected_index"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise DomainError(f"{name} must be non-negative or None")
        if not isinstance(self.volume, int) or not 0 <= self.volume <= 130:
            raise DomainError("volume must be an integer in 0..130")
        if not isinstance(self.mic_level, (int, float)) or not 0 <= self.mic_level <= 1:
            raise DomainError("mic_level must be in 0..1")
        if self.selected_source not in {None, "voice", "browse", "playlist"}:
            raise DomainError("unknown selection source")

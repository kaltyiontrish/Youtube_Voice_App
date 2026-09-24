"""Playback coordination boundary for search results and saved queues.

This service deliberately keeps command matching and UI state outside the
playback boundary. It translates the existing yt-dlp and mpv implementations
without changing their current queue or preload behavior.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from .domain import PlaybackEvent, PlaybackState

from .player import MpvPlayer, PlayerError
from .search import SearchResult, Searcher

LOGGER = logging.getLogger(__name__)


class PlaybackService:
    """Coordinate search, canonical track conversion, and player queue calls."""

    def __init__(self, player: MpvPlayer, searcher: Searcher) -> None:
        self.player = player
        self.searcher = searcher
        self._lock = threading.RLock()
        self._generation = 0
        self._request_id = 0
        self._last_queue: tuple[SearchResult, ...] = ()
        self._last_start_index = 0
        self._last_error: str | None = None

    def ensure_player(self) -> None:
        if not self.player.alive:
            LOGGER.warning("mpv is not running; restarting it")
            self.player.close()
            self.player.start()

    def _begin_request(self, queue: tuple[SearchResult, ...]) -> int:
        with self._lock:
            self._generation += 1
            self._request_id += 1
            self._last_queue = queue
            self._last_start_index = 0
            self._last_error = None
            return self._request_id

    def _transport(self, method: str) -> bool:
        with self._lock:
            return bool(getattr(self.player, method)())

    def next_track(self) -> bool:
        with self._lock:
            try:
                current = self.player.current_position()
            except (AttributeError, PlayerError):
                current = 0
            if current is None and self._last_queue:
                start = min(self._last_start_index + 1, len(self._last_queue) - 1)
                self.player.play_results(self._last_queue, self.searcher, start_index=start)
                return True
            return self._transport("next_track")

    def prev_track(self) -> bool:
        with self._lock:
            try:
                current = self.player.current_position()
            except (AttributeError, PlayerError):
                current = 0
            if current is None and self._last_queue:
                start = max(self._last_start_index - 1, 0)
                self.player.play_results(self._last_queue, self.searcher, start_index=start)
                return True
            return self._transport("prev_track")

    def jump_to(self, index: int) -> bool:
        with self._lock:
            return bool(self.player.jump_to(index))

    def stop(self) -> bool:
        return self._transport("stop")

    def pause(self) -> bool:
        return self._transport("pause")

    def unpause(self) -> bool:
        return self._transport("unpause")

    def set_volume(self, value: float) -> float:
        with self._lock:
            return float(self.player.set_volume(value))

    def current_position(self) -> int | None:
        with self._lock:
            return self.player.current_position()

    def current_url(self) -> str | None:
        with self._lock:
            return self.player.current_url()

    def current_title(self) -> str | None:
        with self._lock:
            return self.player.current_title()

    def pool_titles(self) -> list[str]:
        with self._lock:
            return self.player.pool_titles()

    def playback_position(self) -> tuple[float, float | None]:
        with self._lock:
            return self.player.playback_position()

    def is_playing(self) -> bool:
        with self._lock:
            return self.player.is_playing()

    def volume(self) -> float:
        with self._lock:
            return self.player.volume()

    def snapshot(self) -> PlaybackEvent:
        """Return one coherent, read-only view for voice/UI consumers."""
        with self._lock:
            try:
                index = self.player.current_position()
                title = self.player.current_title()
                url = self.player.current_url()
                seconds, duration = self.player.playback_position()
                playing = self.player.is_playing()
            except Exception as exc:
                LOGGER.debug("playback snapshot unavailable: %s", exc)
                return PlaybackEvent(
                    self._generation, self._request_id, PlaybackState.FAILED,
                    error=str(exc),
                )
            state = PlaybackState.PLAYING if playing else (
                PlaybackState.PAUSED if index is not None else PlaybackState.STOPPED
            )
            return PlaybackEvent(
                self._generation, self._request_id, state,
                current_index=index, current_url=url,
                position=seconds, duration=duration,
                error=self._last_error,
            )


    def search_and_play(self, query: str) -> tuple[SearchResult, ...]:
        self.ensure_player()
        results = tuple(self.searcher.search(query))
        if results:
            self._begin_request(results)
            self._last_start_index = 0
            self.player.play_results(results, self.searcher)
        return results

    def play_results(
        self, results: Iterable[SearchResult], start_index: int = 0
    ) -> None:
        ordered = tuple(results)
        if not ordered:
            raise PlayerError("nothing to play")
        if not 0 <= start_index < len(ordered):
            raise PlayerError(f"start index {start_index} is out of range")
        self.ensure_player()
        self._begin_request(ordered)
        self._last_start_index = start_index
        self.player.play_results(ordered, self.searcher, start_index=start_index)
        return True

    @staticmethod
    def results_from_playlist(tracks: Iterable[dict]) -> tuple[SearchResult, ...]:
        """Convert persisted playlist rows to canonical search results."""
        results: list[SearchResult] = []
        for track in tracks:
            url = str(track.get("url") or "")
            if not url:
                continue
            video_id = str(track.get("video_id") or url.rsplit("=", 1)[-1])
            results.append(
                SearchResult(
                    video_id=video_id,
                    title=str(track.get("title") or url or "Unknown track"),
                    url=url,
                    duration=track.get("duration"),
                )
            )
        return tuple(results)

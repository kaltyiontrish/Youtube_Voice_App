"""Command handlers (plan2.md Â§6.3 and Â§7).

Every ``action`` named in ``config.yaml`` must resolve to a handler registered
here, and that is checked at startup - an unknown action is a configuration
error, never a runtime surprise.  Adding a new phrase is therefore a YAML edit,
and adding a new *action* is one small function in this module.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .config import Config, ConfigError
from .matcher import Command
from .playback_service import PlaybackService
from .player import MpvPlayer, PlayerError
from .playlists import PlaylistStore
from .search import SearchError, SearchResult, Searcher

LOGGER = logging.getLogger(__name__)

HANDLERS: dict[str, Callable[["CommandRunner", Command], bool]] = {}


def handler(action: str) -> Callable[
    [Callable[["CommandRunner", Command], bool]], Callable[["CommandRunner", Command], bool]
]:
    """Register a function as the handler for *action*."""

    def decorator(
        func: Callable[["CommandRunner", Command], bool]
    ) -> Callable[["CommandRunner", Command], bool]:
        HANDLERS[action] = func
        return func

    return decorator


def known_actions() -> tuple[str, ...]:
    return tuple(sorted(HANDLERS))


def validate_actions(config: Config) -> None:
    """Fail at startup when config.yaml names an action nobody implements."""
    unknown = [spec.action for spec in config.commands if spec.action not in HANDLERS]
    if unknown:
        raise ConfigError(
            f"config.yaml uses unknown action(s): {', '.join(sorted(set(unknown)))}; "
            f"available actions: {', '.join(known_actions())}"
        )
    missing = [action for action in HANDLERS if not any(s.action == action for s in config.commands)]
    if missing:
        LOGGER.warning(
            "no verbs configured for action(s): %s", ", ".join(sorted(missing))
        )


class CommandRunner:
    """Dispatch recognised commands to mpv / yt-dlp."""

    def __init__(self, config: Config, player: MpvPlayer, searcher: Searcher) -> None:
        self.config = config
        self.player = player
        self.searcher = searcher
        self.playback = PlaybackService(player, searcher)
        self.last_play_started: float | None = None
        self.last_query: str = ""
        self.last_results: tuple[str, ...] = ()
        self.last_result_objects: tuple[SearchResult, ...] = ()
        playlist_path = getattr(getattr(config, "player", None), "playlist_path", None)
        self.playlist_store = (
            PlaylistStore(playlist_path) if playlist_path is not None else None
        )
        self._dispatch_lock = threading.Lock()

    # -- guards ---------------------------------------------------------- #

    def _within_play_guard(self, now: float) -> bool:
        """plan2.md Â§8 step 3: ignore triggers right after playback starts."""
        if self.last_play_started is None:
            return False
        elapsed_ms = (now - self.last_play_started) * 1000.0
        return elapsed_ms < self.config.behaviour.ignore_trigger_ms_after_play

    def _ensure_player(self) -> None:
        """Restart mpv if it died (socket loss, crash, user closed it)."""
        self.playback.ensure_player()

    # -- dispatch -------------------------------------------------------- #

    def _playlist_name(self, query: str) -> str | None:
        store = self.playlist_store
        if store is None:
            return None
        if query.strip():
            return query.strip() if query.strip() in store.names() else None
        names = store.names()
        return names[0] if names else None

    def _playlist_results(self, name: str) -> list[SearchResult]:
        return [
            SearchResult(
                video_id=str(track.get("url", "")).rsplit("=", 1)[-1],
                title=str(track.get("title") or track.get("url") or "Unknown track"),
                url=str(track["url"]),
                duration=track.get("duration"),
            )
            for track in self.playlist_store.tracks(name)  # type: ignore[union-attr]
            if track.get("url")
        ]

    def play_playlist(self, results: list, start_index: int) -> bool:
        """Serialize saved-playlist replacement with voice and UI commands."""
        with self._dispatch_lock:
            self.playback.play_results(results, start_index)
            self.last_play_started = time.monotonic()
        return True

    def dispatch(self, command: Command) -> bool:
        """Run one handler; voice and UI callers cannot race through yt-dlp/mpv."""
        with self._dispatch_lock:
            return self._dispatch_locked(command)

    def _dispatch_locked(self, command: Command) -> bool:
        now = time.monotonic()
        if self._within_play_guard(now):
            LOGGER.info(
                "ignoring %s: only %.0f ms since playback started (< %d ms guard)",
                command.action,
                (now - (self.last_play_started or now)) * 1000.0,
                self.config.behaviour.ignore_trigger_ms_after_play,
            )
            return False

        func = HANDLERS.get(command.action)
        if func is None:  # pragma: no cover - validate_actions() runs at startup
            LOGGER.error("no handler registered for action %r", command.action)
            return False

        LOGGER.info(
            "command %s%s (heard: %r, fired because: %s)",
            command.action,
            f" query={command.query!r}" if command.query else "",
            command.text,
            command.reason,
        )
        try:
            return bool(func(self, command))
        except (PlayerError, SearchError) as exc:
            LOGGER.error("%s failed: %s", command.action, exc)
            return False
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("unexpected failure in handler %s", command.action)
            return False


def _play_playlist_position(runner: CommandRunner, query: str) -> bool:
    """Start the saved playlist at a 1-based position."""
    if not query.isdigit():
        return False
    name = runner._playlist_name("")
    results = runner._playlist_results(name) if name else []
    index = int(query) - 1
    if not 0 <= index < len(results):
        LOGGER.info("playlist position %s is out of range", query)
        return False
    runner.playback.play_results(results, index)
    runner.last_play_started = time.monotonic()
    return True


@handler("play")
def _play(runner: CommandRunner, command: Command) -> bool:
    """Search YouTube and play the first hit, queueing the rest."""
    query = command.query.strip()
    if query.isdigit():
        return _play_playlist_position(runner, query)
    results = runner.playback.search_and_play(query)
    if not results:
        LOGGER.warning("no results for %r", query)
        return False
    runner.last_play_started = time.monotonic()
    runner.last_query = query
    runner.last_results = tuple(result.title for result in results)
    runner.last_result_objects = tuple(results)
    LOGGER.info(
        "playing %r (1 of %d): %s",
        query,
        len(results),
        "; ".join(f"{index + 1}. {title}" for index, title in enumerate(runner.last_results)),
    )
    return True


@handler("jump")
def _jump(runner: CommandRunner, command: Command) -> bool:
    """Jump to a 1-based track position in the selected playlist."""
    return _play_playlist_position(runner, command.query.strip())


@handler("add")
def _add(runner: CommandRunner, command: Command) -> bool:
    name = runner._playlist_name(command.query)
    url = runner.playback.current_url()
    title = runner.playback.current_title()
    if name is None or not url:
        return False
    runner.playlist_store.add(name, title or url, url)  # type: ignore[union-attr]
    return True


@handler("remove")
def _remove(runner: CommandRunner, command: Command) -> bool:
    name = runner._playlist_name(command.query)
    if name is None:
        return False
    url = runner.playback.current_url()
    tracks = runner.playlist_store.tracks(name)  # type: ignore[union-attr]
    index = next((i for i, track in enumerate(tracks) if track.get("url") == url), -1)
    if index < 0:
        return False
    runner.playlist_store.remove(name, index)  # type: ignore[union-attr]
    return True


@handler("next")
def _next(runner: CommandRunner, command: Command) -> bool:
    runner._ensure_player()
    return runner.playback.next_track()


@handler("prev")
def _prev(runner: CommandRunner, command: Command) -> bool:
    runner._ensure_player()
    return runner.playback.prev_track()


@handler("stop")
def _stop(runner: CommandRunner, command: Command) -> bool:
    return runner.playback.stop()


@handler("pause")
def _pause(runner: CommandRunner, command: Command) -> bool:
    runner._ensure_player()
    return runner.playback.pause()


@handler("resume")
def _resume(runner: CommandRunner, command: Command) -> bool:
    runner._ensure_player()
    return runner.playback.unpause()


@handler("volume_up")
def _volume_up(runner: CommandRunner, command: Command) -> bool:
    return runner.playback.set_volume(runner.player.volume() + runner.config.player.volume_step)


@handler("volume_down")
def _volume_down(runner: CommandRunner, command: Command) -> bool:
    return runner.playback.set_volume(runner.player.volume() - runner.config.player.volume_step)

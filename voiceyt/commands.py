"""Command handlers (plan2.md §6.3 and §7).

Every ``action`` named in ``config.yaml`` must resolve to a handler registered
here, and that is checked at startup - an unknown action is a configuration
error, never a runtime surprise.  Adding a new phrase is therefore a YAML edit,
and adding a new *action* is one small function in this module.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .config import Config, ConfigError
from .matcher import Command
from .player import MpvPlayer, PlayerError
from .search import SearchError, Searcher

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
        self.last_play_started: float | None = None
        self.last_query: str = ""
        self.last_results: tuple[str, ...] = ()

    # -- guards ---------------------------------------------------------- #

    def _within_play_guard(self, now: float) -> bool:
        """plan2.md §8 step 3: ignore triggers right after playback starts."""
        if self.last_play_started is None:
            return False
        elapsed_ms = (now - self.last_play_started) * 1000.0
        return elapsed_ms < self.config.behaviour.ignore_trigger_ms_after_play

    def _ensure_player(self) -> None:
        """Restart mpv if it died (socket loss, crash, user closed it)."""
        if not self.player.alive:
            LOGGER.warning("mpv is not running; restarting it")
            self.player.close()
            self.player.start()

    # -- dispatch -------------------------------------------------------- #

    def dispatch(self, command: Command) -> bool:
        """Run the handler for *command*; returns True when it did something."""
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


@handler("play")
def _play(runner: CommandRunner, command: Command) -> bool:
    """Search YouTube and play the first hit, queueing the rest."""
    query = command.query.strip()
    if not query:
        LOGGER.info("play without a query: nothing to search for")
        return False
    runner._ensure_player()
    results = runner.searcher.search(query)
    if not results:
        LOGGER.warning("no results for %r", query)
        return False
    runner.player.play_results(results, runner.searcher)
    runner.last_play_started = time.monotonic()
    runner.last_query = query
    runner.last_results = tuple(result.title for result in results)
    LOGGER.info(
        "playing %r (1 of %d): %s",
        query,
        len(results),
        "; ".join(f"{index + 1}. {title}" for index, title in enumerate(runner.last_results)),
    )
    return True


@handler("next")
def _next(runner: CommandRunner, command: Command) -> bool:
    runner._ensure_player()
    return runner.player.next_track()


@handler("stop")
def _stop(runner: CommandRunner, command: Command) -> bool:
    return runner.player.stop()


@handler("volume_up")
def _volume_up(runner: CommandRunner, command: Command) -> bool:
    return runner.player.volume_delta(runner.config.player.volume_step)


@handler("volume_down")
def _volume_down(runner: CommandRunner, command: Command) -> bool:
    return runner.player.volume_delta(-runner.config.player.volume_step)
"""Daemon composition, command callbacks, and live listening mode."""

from __future__ import annotations

import logging
import time

from .commands import CommandRunner, validate_actions
from .config import Config
from .listener import Listener
from .models import ensure_models
from .runtime import RuntimeLifecycle
from .search import SearchError
from .transcripts import PARTIAL, TranscriptLog

LOGGER = logging.getLogger("voiceyt")


def build_matcher(config: Config):
    """The trigger state machine, built from config only (plan2.md Â§6.2)."""
    from .matcher import Matcher

    return Matcher(
        trigger_words=config.trigger.words,
        trigger_window_ms=config.trigger.trigger_window_ms,
        silence_end_ms=config.trigger.silence_end_ms,
        query_max_ms=config.trigger.query_max_ms,
        commands=config.commands,
        require_same_utterance_while_playing=(
            config.behaviour.require_same_utterance_while_playing
        ),
    )


def cmd_listen(config: Config) -> int:
    """``--listen``: M2 - collect the transcript journal, act on nothing."""
    from datetime import datetime

    from .aec import create_canceller
    from .asr.registry import load_backend

    ensure_models(config)
    backend = load_backend(config)
    log = TranscriptLog(config.behaviour.log_path, config.behaviour.log_transcripts).open()
    where = str(log.path) if log.enabled else "(disabled by behaviour.log_transcripts)"
    print(f"listening; no commands will fire. transcripts -> {where}")
    print("Ctrl-C to stop.")

    def on_utterance(text: str, ts: float) -> None:
        log.write(text, backend.name, "final", ts)
        print(f"  {datetime.fromtimestamp(ts).strftime('%H:%M:%S')}  {text}")

    def on_partial(text: str, ts: float) -> None:
        log.write(text, backend.name, PARTIAL, ts)

    listener = Listener(
        config,
        backend,
        on_utterance=on_utterance,
        on_partial=on_partial,
        canceller=create_canceller(config),
    ).open()
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
    finally:
        listener.stop()
        log.close()
        backend.close()
    print(f"journal written to {log.path}")
    return 0


# --------------------------------------------------------------------------- #
# UI callbacks (tray thread -> daemon)
# --------------------------------------------------------------------------- #


def _ui_jump_to_pool(player, ui, index: int) -> None:
    """Overlay row click: jump to that entry of the result pool."""
    titles = player.pool_titles()
    if not 0 <= index < len(titles):
        return  # the pool changed since the click; nothing sensible to do
    player.jump_to(index)
    LOGGER.info("pool click: jumping to track %d (%s)", index + 1, titles[index])
    ui.set_action(f"jump #{index + 1} {titles[index]}")


def _ui_log_pause(ui, paused: bool) -> None:
    """Tray click: say it in the log and on the overlay (audio is dropped
    by ``Listener.pause_event``, not here)."""
    LOGGER.info("paused from the tray" if paused else "resumed from the tray")
    ui.set_action("paused (tray)" if paused else "listening again")


def _ui_switch_mic(listener, ui, index: int) -> None:
    """Tray menu: move the live capture to input device *index*."""
    try:
        name = listener.capture.reopen(index)
    except Exception as exc:
        LOGGER.error("cannot switch to input device %s: %s", index, exc)
        ui.set_error(f"microphone: {exc}")
        return
    LOGGER.info("capturing from %s (device %s)", name, index)
    ui.set_action(f"microphone: {name}")


def _ui_quit(listener) -> None:
    """Stop the listener so the main process reaches its cleanup and returns."""
    LOGGER.info("quit requested from UI")
    listener.stop()


def _ui_browse_query(searcher, ui, query: str) -> None:
    """Return music matches to the UI without changing playback."""
    text = query.strip()
    if not text:
        return
    try:
        results = searcher.search(text, music_only=True)
    except SearchError as exc:
        LOGGER.warning("UI browse failed: %s", exc)
        ui.set_error(f"search: {exc}")
        return
    ui.set_search_results([
        {
            "title": result.title,
            "url": result.url,
            "video_id": result.video_id,
            "duration": result.duration,
        }
        for result in results
    ], query=text)


def _ui_play_playlist(runner, ui, tracks, start_index: int) -> None:
    """Load an entire saved playlist through the same queue as voice search."""
    from .search import SearchResult

    clean = [track for track in tracks if track.get("url")]
    if not 0 <= start_index < len(clean):
        return
    results = [
        SearchResult(
            video_id=str(track.get("url", "")).rsplit("=", 1)[-1],
            title=str(track.get("title") or track.get("url") or "Unknown track"),
            url=str(track["url"]),
            duration=track.get("duration"),
        )
        for track in clean
    ]
    ui.set_action(f"play playlist from #{start_index + 1}")
    runner.play_playlist(results, start_index)


def _ui_play_query(runner, ui, query: str) -> None:
    """Player-tab search box / recent-history click: the same path as a
    spoken ``play`` (the runner's guard and handler do the rest)."""
    from .matcher import Command

    text = query.strip()
    if not text:
        return
    LOGGER.info("play from the UI: %s", text)
    ui.set_action(f"play {text}")
    runner.dispatch(
        Command(action="play", query=text, ts=time.monotonic(), text=text, reason="ui")
    )



def run_daemon(config: Config) -> int:
    """Default mode: listen, match and act (milestones M4, M5 and the UI)."""
    from datetime import datetime

    from .aec import create_canceller
    from .asr.registry import load_backend
    from .player import MpvPlayer
    from .search import Searcher
    from .ui import UiState, start_ui

    validate_actions(config)
    ensure_models(config)
    lifecycle = RuntimeLifecycle()

    backend = load_backend(config)
    lifecycle.register("backend", backend.close)
    player = MpvPlayer(
        mpv_path=config.player.mpv_path,
        ipc_path=config.player.ipc_path,
        audio_only=config.player.audio_only,
        extra_args=config.player.extra_args,
        volume_min=config.player.volume_min,
        volume_max=config.player.volume_max,
        volume_step=config.player.volume_step,
        start_timeout_s=config.player.start_timeout_s,
    )
    lifecycle.register("player", player.close)
    player.start()
    searcher = Searcher(
        results=config.search.browse_results,
        cookies_from_browser=config.search.cookies_from_browser,
        socket_timeout_s=config.search.socket_timeout_s,
    )
    runner = CommandRunner(config, player, searcher)
    matcher = build_matcher(config)
    log = TranscriptLog(config.behaviour.log_path, config.behaviour.log_transcripts).open()
    lifecycle.register("transcript", log.close)

    # ---- UI (overlay + tray): disabled by behaviour.ui = false ----------
    ui = None
    tray = None
    if config.behaviour.ui and config.ui_config:
        ui = UiState(
            player=player,
            playback=runner.playback,
            state_path=config.resolve("./logs/ui_state.json"),
            preview_max=config.search.voice_preview_results,
        )
        saved_volume = ui.snapshot().get("volume", 50)
        try:
            runner.playback.set_volume(int(saved_volume))
            ui.set_volume(int(saved_volume))
        except Exception:
            pass
        ui.set_aec(f"AEC on ({config.aec.backend})" if config.aec.enabled else "")

    def dispatch(commands, ts: float) -> None:
        for command in commands:
            log.note(
                f"action={command.action} query={command.query} reason={command.reason}", ts
            )
            if ui is not None:
                ui.set_action(f"{command.action} {command.query}".strip())
                if command.action == "play" and command.query:
                    ui.note_query(command.query)  # E1: recent-history strip
            played = runner.dispatch(command)
            if ui is not None:
                ui.set_playback_snapshot(runner.playback.snapshot())
            if ui is not None and played and command.action == "play" and command.query:
                ui.set_voice_previews(
                    list(runner.last_results[: config.search.voice_preview_results]),
                    [result.url for result in getattr(runner, "last_result_objects", ())[: config.search.voice_preview_results]],
                )

    def on_utterance(text: str, ts: float) -> None:
        LOGGER.info("heard: %s", text)
        log.write(text, backend.name, "final", ts)
        if ui is not None:
            ui.set_heard(text)
        matcher.set_playing(player.playing)
        dispatch(matcher.feed(text, ts), ts)

    def on_partial(text: str, ts: float) -> None:
        # Partials are journalled so M2 can measure streaming latency, but they
        # never drive the matcher: a growing hypothesis would fire twice.
        log.write(text, backend.name, PARTIAL, ts)

    def on_silence(ts: float, silence_ms: float) -> None:
        matcher.set_playing(player.playing)  # event-tracked, no IPC round trip
        dispatch(matcher.tick(ts, silence_ms), ts)

    listener = Listener(
        config,
        backend,
        on_utterance=on_utterance,
        on_partial=on_partial,
        on_silence=on_silence,
        canceller=create_canceller(config),
        pause_event=ui.paused if ui is not None else None,
    )
    lifecycle.register("listener", listener.stop)
    listener.open()

    if ui is not None:
        ui.capture = listener.capture
        ui.set_backend_name(backend.name)
        ui.set_language(config.asr.language)
        tray = start_ui(
            ui,
            config.ui_config,
            on_pool_click=lambda index: _ui_jump_to_pool(player, ui, index),
            on_pause=lambda: _ui_log_pause(ui, True),
            on_resume=lambda: _ui_log_pause(ui, False),
            on_mic_pick=lambda index: _ui_switch_mic(listener, ui, index),
            on_quit=lambda: _ui_quit(listener),
            app_config=config,
            on_play_query=lambda query: _ui_play_query(runner, ui, query),
            on_play_playlist=lambda tracks, index: _ui_play_playlist(
                runner, ui, tracks, index
            ),
            on_browse_query=lambda query: _ui_browse_query(searcher, ui, query),
        )
        if tray is not None:
            lifecycle.register("tray", tray.stop)
        lifecycle.register("overlay", lambda: ui.overlay_done.wait(timeout=2.0))

    example_trigger = config.trigger.words[0]
    example_verb = next(
        (spec for spec in config.commands if spec.takes_query), config.commands[0]
    ).verbs[0]
    print(
        f"ready. try '{example_trigger} {example_verb} <something>'. "
        f"transcripts -> {log.path}"
    )
    if ui is not None:
        print(
            "overlay + tray icon active: click the tray icon to pause, "
            "right-click it for the microphone menu or to quit"
        )
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
    finally:
        lifecycle.close()
    LOGGER.info("stopped")
    return 0


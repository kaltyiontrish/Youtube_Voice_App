"""CLI entry point (plan2.md §3) and the run loop.

    python -m voiceyt                          # run with config default backend
    python -m voiceyt --backend nemotron       # override backend
    python -m voiceyt --list-devices           # print audio input devices + indices
    python -m voiceyt --download-models all    # or: whisper parakeet nemotron vad
    python -m voiceyt --bench audio.wav        # run all 3 backends on one file
    python -m voiceyt --bench-live 60          # record 60s from mic, then compare

Three extra modes exist because the milestone acceptance tests need them:

    python -m voiceyt --meter                  # M1: live RMS level that reacts to speech
    python -m voiceyt --listen                 # M2: transcribe + log only, no commands
    python -m voiceyt --replay-log FILE        # M3: replay a journal offline
    python -m voiceyt --aec-probe              # M5: what AEC is configured/usable
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

from . import __version__
from .aec import diagnose as aec_diagnose
from .commands import CommandRunner, validate_actions
from .config import Config, ConfigError, load_config
from .models import ALL, DownloadReport, download_targets, ensure_models, summary
from .transcripts import PARTIAL, TranscriptLog, read_entries, replay

LOGGER = logging.getLogger("voiceyt")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: str) -> None:
    """Console logging; the transcript journal is a separate, always-UTF-8 file."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m voiceyt",
        description="Voice-controlled YouTube playback (pt-PT): 'youtube passa <query>'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH", help="configuration file (default: ./config.yaml)")
    parser.add_argument("--backend", metavar="NAME", help="override asr.backend for this run")
    parser.add_argument("--log-level", metavar="LEVEL", help="override behaviour.log_level")
    parser.add_argument("--version", action="version", version=f"voiceyt {__version__}")

    parser.add_argument("--list-devices", action="store_true", help="print input devices and exit")
    parser.add_argument("--meter", action="store_true", help="print a live RMS level and exit on Ctrl-C")
    parser.add_argument("--aec-probe", action="store_true", help="report echo-cancellation status")
    parser.add_argument(
        "--download-models",
        nargs="*",
        metavar="TARGET",
        help=f"download models: {ALL}, whisper, parakeet, nemotron, vad",
    )
    parser.add_argument("--bench", metavar="WAV", help="compare all backends on one audio file")
    parser.add_argument(
        "--bench-live",
        type=float,
        metavar="SECONDS",
        help="record N seconds from the microphone, then compare all backends",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="transcribe and log without acting on commands (M2 transcript log)",
    )
    parser.add_argument(
        "--replay-log",
        metavar="PATH",
        help="replay a transcript journal through the matcher (M3 acceptance test)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config and dispatch to one mode."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level or "INFO")

    # cuBLAS/cuDNN from the pip wheels must be visible to the loaders before any
    # backend is imported.
    from .dlls import enable_cuda_dlls

    added = enable_cuda_dlls()
    if added:
        LOGGER.debug("CUDA library directories: %s", ", ".join(added))

    try:
        config = load_config(args.config, backend=args.backend)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.log_level:
        config = replace(
            config, behaviour=replace(config.behaviour, log_level=args.log_level.upper())
        )
        configure_logging(config.behaviour.log_level)
    LOGGER.debug("config: %s (backend=%s)", config.source, config.asr.backend)

    try:
        if args.list_devices:
            return cmd_list_devices()
        if args.aec_probe:
            return cmd_aec_probe(config)
        if args.download_models is not None:
            return cmd_download_models(config, list(args.download_models))
        if args.bench_live:
            return cmd_bench_live(config, args.bench_live)
        if args.bench:
            return cmd_bench(config, args.bench)
        if args.replay_log:
            return cmd_replay_log(config, args.replay_log)
        if args.meter:
            return cmd_meter(config)
        if args.listen:
            return cmd_listen(config)
        return run_daemon(config)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- #
# simple modes
# --------------------------------------------------------------------------- #


def cmd_list_devices() -> int:
    """``--list-devices``: PortAudio indices, so audio.device can be set."""
    from .audio import list_input_devices

    devices = list_input_devices()
    if not devices:
        print("no input devices found; check the microphone and the OS privacy settings")
        return 1
    print(f"{'idx':>4}  {'ch':>2}  {'def':<4}  {'rate':>6}  host api / name")
    for device in devices:
        marker = "yes" if device.is_default else ""
        print(
            f"{device.index:>4}  {device.channels:>2}  {marker:<4}  "
            f"{device.default_samplerate:>6.0f}  {device.host_api}: {device.name}"
        )
    print("\nSet audio.device in config.yaml to an index or a name substring.")
    return 0


def cmd_aec_probe(config: Config) -> int:
    """``--aec-probe``: §8 status report before deciding to write AEC code."""
    print("echo cancellation status")
    print("-" * 40)
    for line in aec_diagnose(config):
        print(line)
    return 0


def cmd_download_models(config: Config, targets: list[str]) -> int:
    """``--download-models``: fetch models explicitly, never at run time."""
    reports: list[DownloadReport] = download_targets(config, targets)
    print()
    print("download results")
    print("-" * 40)
    for report in reports:
        print(f"  {report.name:<9} {'ok' if report.ok else 'FAILED':<7} {report.detail}")
    print()
    print("model state")
    print("-" * 40)
    print("\n".join(summary(config)))
    return 0 if all(report.ok for report in reports) else 1


# --------------------------------------------------------------------------- #
# diagnostics and benchmark modes
# --------------------------------------------------------------------------- #


def cmd_bench(config: Config, path: str) -> int:
    """``--bench FILE``: all backends on one audio file (plan2.md §9, M2)."""
    from .bench import bench_file, format_results

    source = Path(path)
    if not source.is_file():
        print(f"audio file not found: {source}", file=sys.stderr)
        return 2
    results, _, _ = bench_file(config, source)
    print(format_results(results, source.name))
    return 0 if any(not result.error for result in results) else 1


def cmd_bench_live(config: Config, seconds: float) -> int:
    """``--bench-live SECONDS``: record from the mic, then compare (M2)."""
    from .bench import bench_live, format_results

    if seconds <= 0:
        print("--bench-live needs a positive number of seconds", file=sys.stderr)
        return 2
    results, path = bench_live(config, seconds)
    print(format_results(results, path.name))
    print(f"recording kept at {path}; re-run offline with: python -m voiceyt --bench {path}")
    return 0 if any(not result.error for result in results) else 1


def render_meter(level: float, width: int = 40) -> str:
    """ASCII level bar: RMS mapped from -60 dBFS to 0 dBFS."""
    decibels = 20.0 * math.log10(level) if level > 1e-9 else -90.0
    scaled = max(0.0, min(1.0, (decibels + 60.0) / 60.0))
    filled = int(scaled * width)
    return (
        f"level {level:0.4f}  {decibels:6.1f} dBFS  "
        f"[{'#' * filled}{'.' * (width - filled)}]"
    )


def cmd_meter(config: Config) -> int:
    """``--meter``: M1 acceptance - a live RMS level that reacts to speech."""
    from .audio import AudioCapture

    capture = AudioCapture(device=config.audio.device, sample_rate=config.audio.sample_rate)
    try:
        capture.open()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"listening on {capture.device_name} ({config.audio.sample_rate} Hz mono float32); "
        f"Ctrl-C to stop"
    )
    try:
        while True:
            block = capture.read_block(timeout=0.5)
            if block is None:
                continue
            level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
            print(render_meter(level), end="\r", flush=True)
    finally:
        capture.close()
        print()


def cmd_replay_log(config: Config, path: str) -> int:
    """``--replay-log FILE``: M3 acceptance - offline matcher run."""
    from datetime import datetime

    from .transcripts import read_entries, replay

    validate_actions(config)
    source = Path(path)
    try:
        entries = list(read_entries(source))
    except FileNotFoundError:
        print(f"transcript journal not found: {source}", file=sys.stderr)
        return 2
    if not entries:
        print(f"{source}: no usable records", file=sys.stderr)
        return 2

    utterances = sum(1 for entry in entries if entry.feeds_matcher)
    print(
        f"replaying {len(entries)} record(s) from {source}: "
        f"{utterances} utterance(s), {len(entries) - utterances} note(s)/partial(s)"
    )
    firings, transitions = replay(config, entries)

    print(f"\n{len(firings)} command(s) would fire:")
    for entry, command in firings:
        stamp = datetime.fromtimestamp(entry.ts).isoformat(timespec="milliseconds")
        print(
            f"  {stamp}  {command.action:<11} query={command.query!r}  "
            f"<- {entry.text!r} ({command.reason})"
        )
    if not firings:
        print("  (none - M3 acceptance requires zero false firings on this journal)")

    print(f"\n{len(transitions)} state transition(s):")
    for line in transitions[:200]:
        print(f"  {line}")
    if len(transitions) > 200:
        print(f"  ... {len(transitions) - 200} more (trim the journal to see them)")
    return 0


# --------------------------------------------------------------------------- #
# listening pipeline
# --------------------------------------------------------------------------- #


class Listener:
    """audio -> AEC -> VAD -> ASR -> callbacks (plan2.md §5).

    Batch backends (whisper, parakeet) are handed one complete utterance from the
    VAD segmenter.  The streaming backend (nemotron) receives the live blocks
    while the VAD reports speech, is flushed when the utterance ends, and reports
    its growing hypothesis through ``on_partial`` for the journal.

    ``on_silence(ts, silence_ms)`` fires for every block so the caller can run the
    matcher's timers - that is what ends a query early (``silence_end_ms``).

    ``pause_event`` (optional) is checked on every block: while it is set the
    audio is dropped instead of transcribed, so the UI can mute the assistant
    without stopping the capture or losing the stream clock.

    Audio/VAD imports are deliberately local: ``--list-devices`` must not pay for
    importing onnxruntime.
    """

    def __init__(
        self,
        config: Config,
        backend,
        *,
        on_utterance,
        on_partial=None,
        on_silence=None,
        canceller=None,
        pause_event=None,
    ) -> None:
        from .audio import AudioCapture

        self.config = config
        self.backend = backend
        self.on_utterance = on_utterance
        self.on_partial = on_partial
        self.on_silence = on_silence
        self.canceller = canceller
        self.pause_event = pause_event
        self.capture = AudioCapture(
            device=config.audio.device, sample_rate=config.audio.sample_rate
        )
        self.segmenter = None
        self._samples_seen = 0
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._stopped = False
        self._paused_state = False

    @property
    def stream_ts(self) -> float:
        """Timestamp of the newest sample processed, in seconds."""
        return self._samples_seen / float(self.config.audio.sample_rate)

    @property
    def window_samples(self) -> int:
        """Fallback window length used when the VAD is disabled."""
        return int(self.config.vad.max_utterance_s * self.config.audio.sample_rate)

    # -- lifecycle ------------------------------------------------------- #

    def open(self) -> "Listener":
        if self.config.vad.enabled:
            from .vad import SileroVad, UtteranceSegmenter

            vad = SileroVad(str(self.config.vad.model_path))
            self.segmenter = UtteranceSegmenter(
                vad,
                sample_rate=self.config.audio.sample_rate,
                threshold=self.config.vad.threshold,
                min_speech_ms=self.config.vad.min_speech_ms,
                min_silence_ms=self.config.vad.min_silence_ms,
                max_utterance_s=self.config.vad.max_utterance_s,
            )
        else:
            LOGGER.warning(
                "vad.enabled is false: falling back to %.0fs fixed windows with no "
                "silence detection",
                self.config.vad.max_utterance_s,
            )
        self.capture.open()
        LOGGER.info(
            "listening on %s (%d Hz mono, backend=%s)",
            self.capture.device_name,
            self.config.audio.sample_rate,
            self.backend.name,
        )
        return self

    def stop(self) -> None:
        self._stopped = True
        self.capture.close()

    def run(self) -> None:
        """Blocking capture loop until :meth:`stop` or ``Ctrl-C``."""
        try:
            for block in self.capture.blocks():
                if self._stopped:
                    break
                self.process(block)
        finally:
            self.flush()

    # -- processing ------------------------------------------------------ #

    def process(self, block: np.ndarray) -> None:
        """Handle one captured block: AEC, VAD, then ASR."""
        if self.pause_event is not None and self.pause_event.is_set():
            self._drop_while_paused(block)
            return
        if self._paused_state:  # just resumed: start from a clean slate
            self._paused_state = False
            self._reset_pipeline()
        samples = self.canceller.process(block) if self.canceller is not None else block
        self._samples_seen += samples.size
        if self.segmenter is None:
            self._process_without_vad(samples)
            return

        utterances = self.segmenter.push(samples)
        if self.on_silence is not None:
            self.on_silence(self.stream_ts, self.segmenter.trailing_silence_ms)

        if self.backend.streaming:
            if self.segmenter.speaking:
                self._emit_partial(samples)
            for utterance in utterances:
                self._finish_stream(utterance.end_ts)
        else:
            for utterance in utterances:
                self._finish_batch(utterance.audio, utterance.end_ts)

    def _emit_partial(self, samples: np.ndarray) -> None:
        if self.on_partial is None:
            return
        text = self.backend.feed(samples)
        if text:
            self.on_partial(text, self.stream_ts)

    def _finish_stream(self, ts: float) -> None:
        text = (self.backend.flush() or "").strip()
        self.backend.reset()
        if text:
            self.on_utterance(text, ts)

    def _finish_batch(self, audio: np.ndarray, ts: float) -> None:
        text = self.backend.transcribe(audio).strip()
        self.backend.reset()
        if text:
            self.on_utterance(text, ts)

    def _process_without_vad(self, samples: np.ndarray) -> None:
        """VAD disabled: stream continuously, or batch on fixed-length windows."""
        if self.backend.streaming:
            self._emit_partial(samples)
            self._pending_samples += samples.size
            if self._pending_samples >= self.window_samples:
                self._finish_stream(self.stream_ts)
                self._pending_samples = 0
            return

        self._pending.append(samples)
        self._pending_samples += samples.size
        if self._pending_samples < self.window_samples:
            return
        audio = np.concatenate(self._pending)
        self._pending.clear()
        self._pending_samples = 0
        self._finish_batch(audio, self.stream_ts)

    def flush(self) -> None:
        """Emit whatever is still buffered (shutdown)."""
        if self.pause_event is not None and self.pause_event.is_set():
            return  # nothing was transcribed while paused
        if self.segmenter is not None:
            if self.backend.streaming:
                self._finish_stream(self.stream_ts)
                return
            utterance = self.segmenter.flush()
            if utterance is not None:
                self._finish_batch(utterance.audio, utterance.end_ts)
            return
        if self._pending:
            audio = np.concatenate(self._pending)
            self._pending.clear()
            self._pending_samples = 0
            self._finish_batch(audio, self.stream_ts)

    # -- pause (driven by the tray/overlay) ------------------------------- #

    def _drop_while_paused(self, block: np.ndarray) -> None:
        """Throw *block* away, keeping the stream clock in real time.

        The first block of a pause also resets the pipeline so an utterance
        can never span the pause (VAD state, AEC state, streamed hypothesis).
        """
        if not self._paused_state:
            self._paused_state = True
            self._reset_pipeline()
        self._samples_seen += block.size

    def _reset_pipeline(self) -> None:
        self._pending.clear()
        self._pending_samples = 0
        if self.segmenter is not None:
            self.segmenter.reset()
        if self.canceller is not None:
            self.canceller.reset()
        # Same as _finish_batch: every backend is told to drop its buffers
        # (a no-op for the batch ones, required for the streaming one).
        self.backend.reset()


    # (daemon modes below)


# --------------------------------------------------------------------------- #
# run modes
# --------------------------------------------------------------------------- #


def build_matcher(config: Config):
    """The trigger state machine, built from config only (plan2.md §6.2)."""
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
    """Tray quit: stop capture so ``listener.run()`` returns (shutdown flag
    is already set by the tray)."""
    LOGGER.info("quit from the tray")
    listener.stop()



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

    backend = load_backend(config)
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
    player.start()
    searcher = Searcher(
        results=config.search.results,
        cookies_from_browser=config.search.cookies_from_browser,
        socket_timeout_s=config.search.socket_timeout_s,
    )
    runner = CommandRunner(config, player, searcher)
    matcher = build_matcher(config)
    log = TranscriptLog(config.behaviour.log_path, config.behaviour.log_transcripts).open()

    # ---- UI (overlay + tray): disabled by behaviour.ui = false ----------
    ui = UiState(player=player) if config.behaviour.ui else None
    tray = None

    def dispatch(commands, ts: float) -> None:
        for command in commands:
            log.note(
                f"action={command.action} query={command.query} reason={command.reason}", ts
            )
            if ui is not None:
                ui.set_action(f"{command.action} {command.query}".strip())
            runner.dispatch(command)

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
    ).open()

    if ui is not None:
        ui.capture = listener.capture
        tray = start_ui(
            ui,
            on_pool_click=lambda index: _ui_jump_to_pool(player, ui, index),
            on_pause=lambda: _ui_log_pause(ui, True),
            on_resume=lambda: _ui_log_pause(ui, False),
            on_mic_pick=lambda index: _ui_switch_mic(listener, ui, index),
            on_quit=lambda: _ui_quit(listener),
        )

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
        if tray is not None:
            tray.stop()
        listener.stop()
        log.close()
        player.close()
        backend.close()
    LOGGER.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Non-daemon voiceyt CLI modes and diagnostics."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

from .aec import diagnose as aec_diagnose
from .config import Config
from .models import ALL, DownloadReport, download_targets, ensure_models, summary
from .transcripts import read_entries, replay


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
    """``--aec-probe``: Â§8 status report before deciding to write AEC code."""
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
    """``--bench FILE``: all backends on one audio file (plan2.md Â§9, M2)."""
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


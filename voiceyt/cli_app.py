"""CLI application composition for voiceyt."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from . import __version__
from .config import ConfigError, load_config
from .models import ALL
from .cli import (cmd_aec_probe, cmd_bench, cmd_bench_live, cmd_download_models,
    cmd_list_devices, cmd_meter, cmd_replay_log)
from .daemon import cmd_listen, run_daemon

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


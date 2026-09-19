"""Model download, cache and verification (plan2.md §4).

``python -m voiceyt --download-models all`` uses ``huggingface_hub.snapshot_download``
(through each backend's own downloader) into ``models/<backend>/``.  On startup, if
the selected backend's files are missing, the exact download command is printed
and the daemon exits - it never downloads silently at run time.

The Silero VAD ONNX file is small, so it is fetched straight from the silero-vad
repository to whatever path ``vad.model_path`` points at.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .asr.registry import available_backends, create_backend
from .config import Config, ConfigError

LOGGER = logging.getLogger(__name__)

VAD_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/master/"
    "src/silero_vad/data/silero_vad.onnx"
)
MIN_VAD_BYTES = 100_000  # the real file is ~2.3 MB; anything smaller is an error page
ALL = "all"


@dataclass(frozen=True)
class DownloadReport:
    """Outcome of one model download attempt."""

    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------- #
# VAD model
# --------------------------------------------------------------------------- #


def vad_model_path(config: Config) -> Path:
    """Where ``silero_vad.onnx`` lives for this configuration."""
    return config.vad.model_path


def vad_present(config: Config) -> bool:
    path = vad_model_path(config)
    return path.is_file() and path.stat().st_size >= MIN_VAD_BYTES


def download_vad(config: Config) -> DownloadReport:
    """Fetch ``silero_vad.onnx`` into the configured location."""
    target = vad_model_path(config)
    if vad_present(config):
        return DownloadReport("vad", True, f"already present: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    LOGGER.info("downloading Silero VAD -> %s", target)
    try:
        with urllib.request.urlopen(VAD_URL, timeout=60) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.URLError as exc:
        return DownloadReport("vad", False, f"could not download {VAD_URL}: {exc}")
    if len(payload) < MIN_VAD_BYTES:
        return DownloadReport(
            "vad",
            False,
            f"{VAD_URL} returned only {len(payload)} bytes; expected about 2.3 MB",
        )
    partial.write_bytes(payload)
    partial.replace(target)
    return DownloadReport("vad", True, f"{len(payload)} bytes -> {target}")


# --------------------------------------------------------------------------- #
# backend models
# --------------------------------------------------------------------------- #


def resolve_targets(names: list[str]) -> list[str]:
    """Expand CLI targets: ``all`` -> every backend, plus the VAD model."""
    if not names or ALL in names:
        return [*available_backends(), "vad"]
    targets: list[str] = []
    for name in names:
        cleaned = name.strip().lower()
        if not cleaned:
            continue
        if cleaned not in (*available_backends(), "vad"):
            raise ConfigError(
                f"unknown download target {name!r}; use {ALL}, "
                f"{', '.join(available_backends())} or vad"
            )
        if cleaned not in targets:
            targets.append(cleaned)
    return targets


def download_backend(config: Config, name: str) -> DownloadReport:
    """Download one backend's models, reporting rather than raising."""
    backend = create_backend(name)
    local_dir = getattr(backend, "local_dir", lambda _config: None)(config)
    is_downloaded = getattr(backend, "is_downloaded", None)
    if callable(is_downloaded) and is_downloaded(config):
        return DownloadReport(name, True, f"already present: {local_dir}")
    try:
        backend.download(config)
    except Exception as exc:
        return DownloadReport(name, False, str(exc))
    return DownloadReport(name, True, f"saved to {local_dir}")


def download_targets(config: Config, names: list[str]) -> list[DownloadReport]:
    """Download every requested target, one failure at a time."""
    reports: list[DownloadReport] = []
    for name in resolve_targets(names):
        report = download_vad(config) if name == "vad" else download_backend(config, name)
        LOGGER.info(
            "download %-9s %s  %s", name, "ok" if report.ok else "FAILED", report.detail
        )
        reports.append(report)
    return reports


# --------------------------------------------------------------------------- #
# startup verification
# --------------------------------------------------------------------------- #


def missing_for(config: Config, name: str) -> str | None:
    """Describe what is missing for backend *name*, or ``None`` when ready."""
    backend = create_backend(name)
    is_downloaded = getattr(backend, "is_downloaded", None)
    if callable(is_downloaded) and not is_downloaded(config):
        return f"{name}: model files missing"
    return None


def ensure_models(config: Config) -> None:
    """Fail fast with the exact download command when models are missing."""
    problems: list[str] = []
    missing: list[str] = []

    selected = missing_for(config, config.asr.backend)
    if selected:
        problems.append(f"{selected} (asr.backend = {config.asr.backend})")
        missing.append(config.asr.backend)
    if config.vad.enabled and not vad_present(config):
        problems.append(f"vad: {vad_model_path(config)} is missing")
        missing.append("vad")

    if not problems:
        return
    raise ConfigError(
        "model files are missing:\n  - "
        + "\n  - ".join(problems)
        + f"\nrun: python -m voiceyt --download-models {' '.join(missing)}"
    )


def summary(config: Config) -> list[str]:
    """One line per target: where its models live, and whether they are present."""
    lines: list[str] = []
    for name in (*available_backends(), "vad"):
        if name == "vad":
            path = vad_model_path(config)
            state = "ok" if vad_present(config) else "MISSING"
        else:
            backend = create_backend(name)
            path = backend.local_dir(config)  # type: ignore[attr-defined]
            state = "ok" if backend.is_downloaded(config) else "MISSING"  # type: ignore[attr-defined]
        marker = "<- selected" if name == config.asr.backend else ""
        lines.append(f"  {name:<9} {state:<8} {path} {marker}".rstrip())
    return lines
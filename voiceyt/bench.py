"""Backend comparison harness (plan2.md §9, milestone M2).

``--bench file.wav`` and ``--bench-live 60`` run the *same* audio through all
three backends and print transcript, load time, wall-clock time and real-time
factor side by side.  That table decides ``asr.backend`` in config.yaml: which
backend hears "youtube", "proximo" and "para" correctly, and how fast.

Live recordings are saved under ``logs/`` so a session can be re-run offline with
``--bench``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .asr.registry import available_backends, create_backend
from .config import Config

LOGGER = logging.getLogger(__name__)

TARGET_RATE = 16000
STREAM_BLOCK_MS = 320


@dataclass(frozen=True)
class BenchResult:
    """One backend's result for one audio file."""

    backend: str
    streaming: bool
    transcript: str
    load_s: float
    transcribe_s: float
    audio_s: float
    language: str | None = None
    error: str | None = None

    @property
    def rtf(self) -> float:
        """Real-time factor: audio seconds divided by processing seconds."""
        if self.transcribe_s <= 0:
            return 0.0
        return self.audio_s / self.transcribe_s


def load_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV/FLAC file as mono float32 at 16 kHz."""
    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != TARGET_RATE:
        from scipy.signal import resample_poly

        divisor = int(np.gcd(rate, TARGET_RATE))
        mono = resample_poly(mono, TARGET_RATE // divisor, rate // divisor).astype(np.float32)
        rate = TARGET_RATE
    return np.ascontiguousarray(mono, dtype=np.float32), rate


def transcribe_with(backend, samples: np.ndarray, block_ms: int = STREAM_BLOCK_MS) -> str:
    """Transcribe *samples* with either a batch or a streaming backend."""
    if backend.streaming:
        step = max(1, int(TARGET_RATE * block_ms / 1000))
        for start in range(0, samples.size, step):
            backend.feed(samples[start : start + step])
        text = backend.flush() or ""
        backend.reset()
        return text.strip()
    return backend.transcribe(samples).strip()


def bench_file(
    config: Config,
    path: str | Path,
    backends: list[str] | None = None,
) -> tuple[list[BenchResult], np.ndarray, int]:
    """Run every requested backend over one audio file."""
    samples, rate = load_audio(path)
    audio_s = samples.size / float(rate)
    names = backends or list(available_backends())
    results: list[BenchResult] = []

    for name in names:
        backend = create_backend(name)
        started = time.perf_counter()
        try:
            backend.load(config)
        except Exception as exc:
            results.append(
                BenchResult(
                    backend=name,
                    streaming=backend.streaming,
                    transcript="",
                    load_s=time.perf_counter() - started,
                    transcribe_s=0.0,
                    audio_s=audio_s,
                    error=f"load failed: {exc}",
                )
            )
            continue
        load_s = time.perf_counter() - started

        started = time.perf_counter()
        try:
            text = transcribe_with(backend, samples)
            error = None
        except Exception as exc:  # one broken backend must not hide the others
            text = ""
            error = f"transcribe failed: {exc}"
        transcribe_s = time.perf_counter() - started
        results.append(
            BenchResult(
                backend=name,
                streaming=backend.streaming,
                transcript=text,
                load_s=load_s,
                transcribe_s=transcribe_s,
                audio_s=audio_s,
                language=getattr(backend, "last_language", None),
                error=error,
            )
        )
        backend.close()
    return results, samples, rate


def record_live(config: Config, seconds: float) -> tuple[np.ndarray, Path]:
    """Record *seconds* from the microphone and save it under ``logs/``."""
    from .audio import AudioCapture

    capture = AudioCapture(device=config.audio.device, sample_rate=config.audio.sample_rate)
    print(f"Recording {seconds:.0f}s from {capture.device_spec or 'the default mic'}...")
    with capture:
        samples = capture.record(seconds)
    out_dir = config.behaviour.log_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bench-live-{time.strftime('%Y%m%d-%H%M%S')}.wav"
    sf.write(str(path), samples, config.audio.sample_rate, subtype="PCM_16")
    LOGGER.info("saved %s (%.1fs)", path, samples.size / config.audio.sample_rate)
    return samples, path


def bench_live(
    config: Config, seconds: float, backends: list[str] | None = None
) -> tuple[list[BenchResult], Path]:
    """Record from the mic, then compare all backends on that recording."""
    _, path = record_live(config, seconds)
    results, _, _ = bench_file(config, path, backends)
    return results, path


def format_results(results: list[BenchResult], source: str) -> str:
    """Human-readable comparison table."""
    lines = ["", f"bench: {source}", "=" * 78]
    for result in results:
        header = f"{result.backend} ({'streaming' if result.streaming else 'batch'})"
        lines.append(header)
        lines.append("-" * len(header))
        if result.error:
            lines.append(f"  ERROR       : {result.error}")
            lines.append("")
            continue
        lines.append(f"  transcript  : {result.transcript or '(empty)'}")
        lines.append(f"  language    : {result.language or '(unknown)'}")
        lines.append(
            f"  load        : {result.load_s:6.2f} s    "
            f"transcribe: {result.transcribe_s:6.2f} s    "
            f"RTF: {result.rtf:6.2f}   (audio {result.audio_s:.1f} s)"
        )
        lines.append("")
    lines.append("=" * 78)
    ok = [item for item in results if not item.error]
    if ok:
        fastest = max(ok, key=lambda item: item.rtf)
        lines.append(f"fastest: {fastest.backend} (RTF {fastest.rtf:.2f})")
    lines.append(
        "Compare transcripts by eye for 'youtube', 'proximo' and 'para', then set "
        "asr.backend to the winner."
    )
    return "\n".join(lines)
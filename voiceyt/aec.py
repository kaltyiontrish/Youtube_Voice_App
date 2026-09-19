"""Optional echo cancellation (plan2.md §8 step 2).

The default is to keep listening while music plays: gating the recogniser would
make ``youtube para`` impossible.  The cheap safety net from §8 step 3 lives in
``matcher.py``/``commands.py``; this module is the heavier, optional, in-process
AEC that only runs when ``aec.enabled: true``.  The daemon must keep working with
``aec.enabled: false`` - that is a hard requirement of the plan.

``aec.backend`` selects the engine:

``echoff``
    Synchronises the system-audio loopback with mic capture and hands back
    matched frames, doing the timestamp alignment for you.  It *owns* capture, so
    it is an audio source (see :class:`EchoffSource`) rather than an in-line
    filter; its live path is Windows-only and it needs ~7.5 s of paired audio
    before it reports ``echo_path_ready``.
``voiceclean``
    Pure NumPy AEC: ``feed_reference()`` the playback signal, ``process()`` the
    mic signal.  No C libraries, telephony-oriented, int16 internally.
``speexdsp``
    Speex echo canceller.  You must supply the far-end reference yourself and
    keep it frame-aligned; misalignment makes AEC useless.

Every in-line backend needs the *playback* signal, captured from a WASAPI
loopback device (``aec.loopback_device``), not just the microphone.
"""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from .config import Config

LOGGER = logging.getLogger(__name__)


class AecError(RuntimeError):
    """Raised when the configured echo canceller cannot be built."""


class EchoCanceller(Protocol):
    """In-line canceller: mic in, echo-reduced mic out."""

    name: str

    def feed_reference(self, reference: np.ndarray) -> None: ...
    def process(self, mic: np.ndarray) -> np.ndarray: ...
    def reset(self) -> None: ...


def to_int16(samples: np.ndarray) -> np.ndarray:
    """float32 [-1, 1] -> int16, which voiceclean and Speex both expect."""
    clipped = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def to_float(samples: np.ndarray) -> np.ndarray:
    """int16 -> float32 [-1, 1]."""
    return (np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0).reshape(-1)


class PassthroughCanceller:
    """What ``aec.enabled: false`` means in practice: the mic untouched."""

    name = "passthrough"

    def feed_reference(self, reference: np.ndarray) -> None:  # noqa: ARG002
        return None

    def process(self, mic: np.ndarray) -> np.ndarray:
        return np.asarray(mic, dtype=np.float32).reshape(-1)

    def reset(self) -> None:
        return None


class VoicecleanCanceller:
    """``voiceclean``: cross-correlation echo detection + spectral masking."""

    name = "voiceclean"

    def __init__(
        self,
        sample_rate: int,
        chunk_ms: int = 40,
        buffer_ms: int = 800,
        threshold: float = 0.15,
        suppress_db: float = -30.0,
    ) -> None:
        try:
            from voiceclean.aec import AEC
        except ImportError as exc:
            raise AecError(
                "voiceclean is not installed; run: pip install voiceclean"
            ) from exc
        self._aec = AEC(
            sample_rate=sample_rate,
            chunk_ms=chunk_ms,
            buffer_ms=buffer_ms,
            correlation_threshold=threshold,
            suppress_db=suppress_db,
        )

    def feed_reference(self, reference: np.ndarray) -> None:
        self._aec.feed_reference(to_int16(reference))

    def process(self, mic: np.ndarray) -> np.ndarray:
        return to_float(self._aec.process(to_int16(mic)))

    def reset(self) -> None:
        return None


class SpeexdspCanceller:
    """``speexdsp``: stable Speex AEC; the caller owns the reference and alignment."""

    name = "speexdsp"

    def __init__(
        self,
        sample_rate: int,
        frame_ms: int = 10,
        filter_ms: int = 200,
        stream_delay_ms: int = 0,
    ) -> None:
        try:
            from speexdsp import EchoCanceller as SpeexEchoCanceller
        except ImportError as exc:
            raise AecError(
                "speexdsp is not installed; run: pip install speexdsp "
                "(needs libspeexdsp available on the system)"
            ) from exc
        self.frame_size = max(1, int(sample_rate * frame_ms / 1000))
        self._ec = self._build(
            SpeexEchoCanceller,
            self.frame_size,
            max(self.frame_size, int(sample_rate * filter_ms / 1000)),
            sample_rate,
            stream_delay_ms,
        )
        self._pending_ref = np.zeros(0, dtype=np.int16)

    @staticmethod
    def _build(
        cls, frame_size: int, filter_length: int, sample_rate: int, delay_ms: int
    ) -> object:
        """speexdsp changed its constructor across releases; support both shapes."""
        for attempt in (
            lambda: cls.create(
                frame_size=frame_size,
                filter_length=filter_length,
                sample_rate=sample_rate,
                stream_delay_ms=delay_ms,
            ),
            lambda: cls(
                frame_size=frame_size,
                filter_length=filter_length,
                sample_rate=sample_rate,
                stream_delay_ms=delay_ms,
            ),
            lambda: cls(frame_size, filter_length, sample_rate),
        ):
            try:
                return attempt()
            except (AttributeError, TypeError):
                continue
            except Exception as exc:  # pragma: no cover - native library failure
                raise AecError(f"speexdsp could not be initialised: {exc}") from exc
        raise AecError(
            "unsupported speexdsp API; voiceyt expects EchoCanceller.create(frame_size, "
            "filter_length, sample_rate, stream_delay_ms) or the matching keyword form"
        )

    def feed_reference(self, reference: np.ndarray) -> None:
        self._pending_ref = np.concatenate((self._pending_ref, to_int16(reference)))

    def process(self, mic: np.ndarray) -> np.ndarray:
        mic_i16 = to_int16(mic)
        frames = mic_i16.size // self.frame_size
        if frames == 0:
            return to_float(mic_i16)
        cleaned: list[np.ndarray] = []
        for index in range(frames):
            start = index * self.frame_size
            mic_frame = mic_i16[start : start + self.frame_size]
            if self._pending_ref.size >= self.frame_size:
                ref_frame = self._pending_ref[: self.frame_size]
                self._pending_ref = self._pending_ref[self.frame_size :]
            else:
                # A missing reference would desynchronise the filter, and the plan
                # is explicit that misalignment makes AEC useless: silence is safer.
                ref_frame = np.zeros(self.frame_size, dtype=np.int16)
            result = self._ec.cancel(mic_frame.tobytes(), ref_frame.tobytes())
            cleaned.append(np.frombuffer(result, dtype=np.int16))
        return to_float(np.concatenate(cleaned))

    def reset(self) -> None:
        self._pending_ref = np.zeros(0, dtype=np.int16)


ECHOFF_NOTE = (
    "aec.backend 'echoff' is not wired in as an in-line filter: echoff owns its own "
    "WASAPI-loopback capture and returns aligned reference/mic/clean frames, and its "
    "live capture is Windows-only. Do plan2.md §8 step 1 first (OS-level AEC or "
    "headphones); if that is not enough, use echoff's capture as the audio source in "
    "M5 step 2. Meanwhile the microphone passes through untouched."
)


def create_canceller(config: Config) -> EchoCanceller:
    """Build the configured canceller, falling back to passthrough with a warning."""
    if not config.aec.enabled:
        return PassthroughCanceller()
    backend = config.aec.backend
    if backend == "voiceclean":
        return VoicecleanCanceller(
            config.audio.sample_rate,
            chunk_ms=max(10, config.aec.frame_ms * 4),
        )
    if backend == "speexdsp":
        return SpeexdspCanceller(
            config.audio.sample_rate,
            frame_ms=config.aec.frame_ms,
            stream_delay_ms=config.aec.stream_delay_ms,
        )
    LOGGER.warning(ECHOFF_NOTE)
    return PassthroughCanceller()


def diagnose(config: Config) -> list[str]:
    """Lines printed by ``--aec-probe``: what is configured and what is usable."""
    lines = [
        f"aec.enabled : {config.aec.enabled}",
        f"aec.backend : {config.aec.backend}",
        f"loopback    : {config.aec.loopback_device} (WASAPI loopback index)",
        f"frame_ms    : {config.aec.frame_ms}",
    ]
    for module, hint in (
        ("voiceclean", "pip install voiceclean"),
        ("speexdsp", "pip install speexdsp (libspeexdsp)"),
        ("echoff", "pip install echoff (Windows, alpha)"),
    ):
        try:
            __import__(module)
            lines.append(f"{module:<12}: importable")
        except ImportError:
            lines.append(f"{module:<12}: not installed ({hint})")
    lines.append(
        "Reminder: plan2.md §8 step 1 is zero code - OS-level AEC, the capture "
        "device's voice-communication mode, or headphones."
    )
    return lines
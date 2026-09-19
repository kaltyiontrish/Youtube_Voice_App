"""Nemotron 3.5 streaming backend (plan2.md §4).

``nvidia/nemotron-3.5-asr-streaming-0.6b`` is a cache-aware FastConformer-RNNT
with 80/160/320/560/1120 ms chunks, native punctuation and capitalization, and
automatic language detection.  It is the only backend that can match the trigger
*mid-sentence* instead of after the speaker stops.

There is no onnx-asr or NeMo support for this model, so this backend drives the
community ONNX export ``codavidgarcia/nemotron-3.5-asr-streaming-0.6b-onnx``
through the vendored NumPy + onnxruntime engine
(``voiceyt/vendor/nemotron_onnx_streaming.py``, Apache-2.0 code; the weights stay
NVIDIA's under OpenMDW-1.1 - see NOTICE).

Partial/final semantics:
    ``feed`` returns the growing hypothesis whenever it changes and ``flush``
    returns the final transcript when the utterance ends.  The run loop matches
    on the flushed text, so a growing hypothesis cannot fire the same command
    twice; partials are still logged (``--listen``) so their latency advantage
    can be measured during M2.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import numpy as np

from ..config import Config
from .base import AsrError, BaseBackend

LOGGER = logging.getLogger(__name__)

ENGINE_LOCATION = "voiceyt/vendor/nemotron_onnx_streaming.py"
VENDOR_REPO = "github.com/codavidgarcia/nemotron-3.5-asr-streaming-onnx"
REQUIRED_FILES = ("decoder.onnx", "joiner.onnx", "tokens.txt", "nemotron_onnx_config.json")
DOWNLOAD_PATTERNS = (
    "*.onnx",
    "*.onnx.data",
    "tokens.txt",
    "nemotron_onnx_config.json",
    "NOTICE",
    "LICENSE*",
)


def load_engine_class() -> type:
    """Import the vendored streaming engine, with a fix-it message if absent."""
    try:
        from ..vendor.nemotron_onnx_streaming import NemotronOnnxStreaming
    except ImportError as exc:
        raise AsrError(
            f"the vendored Nemotron streaming engine is missing ({ENGINE_LOCATION}); copy "
            f"engine/nemotron_onnx_streaming.py from {VENDOR_REPO} into voiceyt/vendor/"
        ) from exc
    return NemotronOnnxStreaming


class NemotronBackend(BaseBackend):
    """Streaming Nemotron ASR; the only backend with ``streaming = True``."""

    name = "nemotron"
    streaming = True

    def __init__(self) -> None:
        super().__init__()
        self._engine = None
        self._last_partial: str | None = None

    # -- paths ----------------------------------------------------------- #

    def local_dir(self, config: Config) -> Path:
        return self.model_root(config) / "nemotron-streaming"

    def encoder_files(self, config: Config) -> tuple[str, str]:
        settings = config.asr.nemotron
        return (
            f"encoder_{settings.chunk_ms}ms_{settings.precision}.onnx",
            f"encoder_{settings.chunk_ms}ms_first_{settings.precision}.onnx",
        )

    def is_downloaded(self, config: Config) -> bool:
        directory = self.local_dir(config)
        if not directory.is_dir():
            return False
        if any(not (directory / name).is_file() for name in REQUIRED_FILES):
            return False
        return all((directory / name).is_file() for name in self.encoder_files(config))

    # -- model download -------------------------------------------------- #

    def download(self, config: Config) -> None:
        """Fetch the fp16 ONNX export (~2.5 GB) into ``models/nemotron``."""
        from huggingface_hub import snapshot_download

        repo_id = config.asr.nemotron.model_id
        target = self.local_dir(config)
        target.mkdir(parents=True, exist_ok=True)
        LOGGER.info("downloading %s (about 2.5 GB) -> %s", repo_id, target)
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target),
                allow_patterns=list(DOWNLOAD_PATTERNS),
            )
        except Exception as exc:
            raise AsrError(f"could not download {repo_id}: {exc}") from exc

    # -- lifecycle ------------------------------------------------------- #

    def load(self, config: Config) -> None:
        engine_class = load_engine_class()
        self.config = config
        settings = config.asr.nemotron
        directory = self.local_dir(config)

        kwargs: dict[str, object] = {
            "language": settings.language,
            "chunk_ms": settings.chunk_ms,
            "precision": settings.precision,
        }
        # The vendored engine does not document a providers argument; pass one
        # only when the installed copy accepts it.
        if "providers" in inspect.signature(engine_class.__init__).parameters:
            kwargs["providers"] = self.providers(config)

        LOGGER.info(
            "loading nemotron %s (chunk_ms=%s, precision=%s, language=%s)",
            directory,
            settings.chunk_ms,
            settings.precision,
            settings.language,
        )
        try:
            self._engine = engine_class(str(directory), **kwargs)
        except Exception as exc:
            raise AsrError(f"could not start the Nemotron ONNX engine: {exc}") from exc
        self._loaded = True

    # -- streaming inference --------------------------------------------- #

    def feed(self, pcm: np.ndarray) -> str | None:
        """Push audio and return the new hypothesis, or ``None`` if unchanged."""
        if self._engine is None:
            raise AsrError("nemotron backend is not loaded")
        samples = self.to_pcm(pcm)
        if samples.size == 0:
            return None
        try:
            self._engine.accept_waveform(samples)
        except Exception as exc:
            raise AsrError(f"nemotron streaming inference failed: {exc}") from exc
        try:
            text = str(self._engine.get_partial() or "").strip()
        except Exception as exc:  # pragma: no cover - vendored engine
            raise AsrError(f"nemotron get_partial() failed: {exc}") from exc
        if text == self._last_partial:
            return None
        self._last_partial = text
        return text or None

    def flush(self) -> str:
        """Flush the tail of the current utterance and return its final text."""
        if self._engine is None:
            raise AsrError("nemotron backend is not loaded")
        try:
            text = str(self._engine.get_final() or "").strip()
        except Exception as exc:  # pragma: no cover - vendored engine
            raise AsrError(f"nemotron get_final() failed: {exc}") from exc
        self._last_partial = None
        language = self.detected_language
        if language:
            self.last_language = language
        return text

    @property
    def detected_language(self) -> str | None:
        """Language tag reported by the model, e.g. ``<pt-PT>`` in auto mode."""
        value = getattr(self._engine, "detected_language", None)
        return str(value) if value else None

    @property
    def realtime_factor(self) -> float | None:
        """Engine-reported RTF, when the vendored engine exposes it."""
        value = getattr(self._engine, "rtf", None)
        return float(value) if isinstance(value, (int, float)) else None

    def reset(self) -> None:
        super().reset()
        self._last_partial = None
        resetter = getattr(self._engine, "reset", None)
        if callable(resetter):
            try:
                resetter()
            except Exception:  # pragma: no cover - vendored engine
                LOGGER.debug("nemotron engine reset failed", exc_info=True)
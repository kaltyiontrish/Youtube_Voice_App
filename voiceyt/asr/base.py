"""The ASR backend interface every engine implements (plan2.md §4).

    class ASRBackend(Protocol):
        name: str
        streaming: bool
        def load(self, cfg) -> None: ...
        def transcribe(self, pcm: np.ndarray) -> str: ...   # batch backends
        def feed(self, pcm: np.ndarray) -> str | None: ...  # streaming backends
        def reset(self) -> None: ...

Batch backends (whisper, parakeet) are fed one complete utterance by the VAD;
the streaming backend (nemotron) gets continuous chunks and uses the VAD only as
a power gate.  ``download`` is part of the interface so the model downloader
never has to know how a backend stores its files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from ..config import Config


class AsrError(RuntimeError):
    """Raised when a backend cannot load, download or transcribe."""


@dataclass(frozen=True)
class Transcription:
    """Result of one batch transcription."""

    text: str
    language: str | None = None


@runtime_checkable
class ASRBackend(Protocol):
    name: str
    streaming: bool

    def download(self, config: Config) -> None: ...
    def load(self, config: Config) -> None: ...
    def transcribe(self, pcm: np.ndarray) -> str: ...
    def feed(self, pcm: np.ndarray) -> str | None: ...
    def reset(self) -> None: ...


class BaseBackend:
    """Shared plumbing; subclasses override what they actually support."""

    name = "base"
    streaming = False

    def __init__(self) -> None:
        self.config: Config | None = None
        self.last_language: str | None = None
        self._loaded = False

    # -- state ----------------------------------------------------------- #

    @property
    def loaded(self) -> bool:
        return self._loaded

    # -- lifecycle ------------------------------------------------------- #

    def download(self, config: Config) -> None:
        raise AsrError(f"{self.name}: this backend has no model downloader")

    def load(self, config: Config) -> None:
        raise AsrError(f"{self.name}: load() is not implemented")

    def transcribe(self, pcm: np.ndarray) -> str:
        raise AsrError(f"{self.name} is a streaming backend; use feed()")

    def feed(self, pcm: np.ndarray) -> str | None:
        raise AsrError(f"{self.name} is a batch backend; use transcribe()")

    def reset(self) -> None:
        """Clear any per-utterance state; harmless default."""
        self.last_language = None

    def flush(self) -> str:
        """Return buffered tail text; only streaming backends produce any."""
        return ""

    def close(self) -> None:
        self._loaded = False

    # -- helpers --------------------------------------------------------- #

    @staticmethod
    def to_pcm(pcm: np.ndarray) -> np.ndarray:
        """Normalize any input to 1-D contiguous float32 in [-1, 1]."""
        samples = np.asarray(pcm, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples.reshape(-1)
        return np.ascontiguousarray(samples)

    def model_root(self, config: Config) -> Path:
        """``models/<backend>/`` - the directory this backend owns."""
        return config.asr.models_dir / self.name

    @staticmethod
    def providers(config: Config) -> list[str]:
        """onnxruntime providers honouring ``asr.device``."""
        if config.asr.device == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]
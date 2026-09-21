"""Backend registry: name -> class (plan2.md §4).

Adding a backend is one new module plus one line in ``BACKENDS``; nothing else in
the codebase names a backend.  Imports here stay cheap because every heavy
dependency (faster-whisper, onnx-asr, the Nemotron engine) is imported inside the
backend's own methods.
"""

from __future__ import annotations

from ..config import Config, ConfigError
from .base import ASRBackend, AsrError, BaseBackend
from .nemotron import NemotronBackend
from .parakeet import ParakeetBackend
from .vosk import VoskBackend
from .whisper import WhisperBackend

BACKENDS: dict[str, type[BaseBackend]] = {
    WhisperBackend.name: WhisperBackend,
    ParakeetBackend.name: ParakeetBackend,
    NemotronBackend.name: NemotronBackend,
    VoskBackend.name: VoskBackend,
}


def available_backends() -> tuple[str, ...]:
    """Names accepted by ``--backend`` and ``asr.backend``."""
    return tuple(sorted(BACKENDS))


def create_backend(name: str) -> BaseBackend:
    """Instantiate (but do not load) the named backend."""
    try:
        backend_class = BACKENDS[name]
    except KeyError:
        raise ConfigError(
            f"unknown ASR backend {name!r}; available: {', '.join(available_backends())}"
        ) from None
    return backend_class()


def load_backend(config: Config) -> BaseBackend:
    """Create and load the backend named by the configuration."""
    backend = create_backend(config.asr.backend)
    backend.load(config)
    return backend


__all__ = [
    "ASRBackend",
    "AsrError",
    "BaseBackend",
    "BACKENDS",
    "available_backends",
    "create_backend",
    "load_backend",
]
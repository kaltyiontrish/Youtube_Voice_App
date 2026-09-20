"""Parakeet TDT 0.6B v3 backend via ``onnx-asr`` (plan2.md §4).

``onnx-asr`` runs NVIDIA's multilingual Parakeet export with plain
``onnxruntime`` - no NeMo, which would drag in a very large dependency tree that
does not belong in a background daemon.  Parakeet v3 covers 25 European
languages with automatic language detection, and its Portuguese training data is
European rather than Brazilian.

Also batch by design, so it runs per VAD utterance.  The maximum audio length for
this model family is ~20-30 s, which is what ``vad.max_utterance_s`` guards.

Plan-B if accuracy disappoints: ``yuriyvnv/parakeet-tdt-0.6b-portuguese``, but it
is published as a NeMo checkpoint, so it would have to be converted to ONNX
first; ``asr.parakeet.model_id`` accepts a local ONNX directory for that case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import Config
from .base import AsrError, BaseBackend

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterSupport:
    """What the loaded onnx-asr adapter can do."""

    batch: bool
    reason: str


class ParakeetBackend(BaseBackend):
    """Parakeet TDT 0.6B v3 through onnx-asr."""

    name = "parakeet"
    streaming = False

    def __init__(self) -> None:
        super().__init__()
        self._adapter = None
        self.support = AdapterSupport(batch=False, reason="not loaded")

    # -- paths ----------------------------------------------------------- #

    def local_dir(self, config: Config) -> Path:
        return self.model_root(config) / "parakeet-tdt-0.6b-v3"

    def is_downloaded(self, config: Config) -> bool:
        directory = self.local_dir(config)
        return directory.is_dir() and any(directory.glob("*.onnx"))

    # -- model download -------------------------------------------------- #

    def download(self, config: Config) -> None:
        """Fetch the ONNX graphs into ``models/parakeet``.

        onnx-asr's ``load_model(model, path)`` is local-only in the pinned
        version (its download fallback does not catch ModelFileNotFoundError),
        so the files are fetched with snapshot_download using the same repo
        onnx-asr maps the model id to (resolver.py REPO_IDS).
        """
        import onnx_asr  # noqa: F401 - keeps the import-error surface identical
        from huggingface_hub import snapshot_download

        settings = config.asr.parakeet
        target = self.local_dir(config)
        target.mkdir(parents=True, exist_ok=True)
        LOGGER.info("downloading %s -> %s", settings.model_id, target)
        try:
            from onnx_asr.resolver import model_repos

            repo = model_repos[settings.model_id]
        except (ImportError, KeyError):  # pragma: no cover - version drift
            repo = "istupakov/parakeet-tdt-0.6b-v3-onnx"
        try:
            snapshot_download(
                repo,
                local_dir=target,
                allow_patterns=["*.onnx*", "*.ort", "*.json", "*.yaml", "*.txt"],
            )
        except Exception as exc:
            raise AsrError(f"could not download {settings.model_id}: {exc}") from exc

    # -- lifecycle ------------------------------------------------------- #

    def load(self, config: Config) -> None:
        import onnx_asr

        self.config = config
        settings = config.asr.parakeet
        path: str | None = str(self.local_dir(config)) if self.is_downloaded(config) else None
        LOGGER.info(
            "loading parakeet model=%s path=%s device=%s",
            settings.model_id,
            path or "(huggingface cache, will download)",
            config.asr.device,
        )
        try:
            self._adapter = onnx_asr.load_model(
                settings.model_id,
                path,
                quantization=settings.quantization,
                providers=self.providers(config),
            )
        except Exception as exc:
            raise AsrError(
                f"could not load {settings.model_id} via onnx-asr: {exc}"
            ) from exc
        self.support = self._detect_support(self._adapter)
        LOGGER.debug("onnx-asr adapter: %s", self.support)
        self._loaded = True

    @staticmethod
    def _detect_support(adapter: object) -> AdapterSupport:
        """Decide whether ``batch_size`` may be passed to ``recognize``.

        The plain ``AsrAdapter.recognize`` takes ``batch_size``; the VAD/timestamp
        adapters take one waveform per call.  Checking the class name keeps this
        working across onnx-asr releases instead of catching a TypeError at run
        time.
        """
        name = type(adapter).__name__
        if name == "AsrAdapter":
            return AdapterSupport(batch=True, reason="plain AsrAdapter supports batch_size")
        return AdapterSupport(batch=False, reason=f"{name} takes one waveform per call")

    # -- inference ------------------------------------------------------- #

    def transcribe(self, pcm: np.ndarray) -> str:
        if self._adapter is None:
            raise AsrError("parakeet backend is not loaded")
        samples = self.to_pcm(pcm)
        if samples.size == 0:
            return ""
        language = self.config.asr.language if self.config else "pt"
        try:
            text = self._adapter.recognize(samples, sample_rate=16000, language=language)
        except Exception as exc:
            raise AsrError(f"parakeet transcription failed: {exc}") from exc
        return str(text or "").strip()

    def transcribe_batch(self, chunks: list[np.ndarray]) -> list[str]:
        """Transcribe several utterances at once when the adapter allows it."""
        if self._adapter is None:
            raise AsrError("parakeet backend is not loaded")
        if not self.support.batch or len(chunks) <= 1:
            return [self.transcribe(chunk) for chunk in chunks]
        samples = [self.to_pcm(chunk) for chunk in chunks]
        try:
            results = self._adapter.recognize(samples, sample_rate=16000)
        except Exception as exc:
            raise AsrError(f"parakeet batch transcription failed: {exc}") from exc
        return [str(item or "").strip() for item in results]
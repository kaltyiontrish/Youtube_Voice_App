"""faster-whisper backend (plan2.md §4).

Whisper is batch by design (30 s windows), so it runs per utterance behind the
VAD.  ``language="pt"`` is forced: auto-detection drifts to Spanish on short
clips, which would break the trigger word.

GPU needs cuBLAS 12 + cuDNN 9; ``voiceyt.dlls`` makes the pip wheels visible.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..config import Config
from .base import AsrError, BaseBackend

LOGGER = logging.getLogger(__name__)

REPO_TEMPLATE = "Systran/faster-whisper-{model}"
DISK_MODEL_FILE = "model.bin"


class WhisperBackend(BaseBackend):
    """CTranslate2 build of Whisper, running per VAD utterance."""

    name = "whisper"
    streaming = False

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    # -- paths ----------------------------------------------------------- #

    def local_dir(self, config: Config) -> Path:
        model = config.asr.whisper.model
        return self.model_root(config) / f"faster-whisper-{Path(model).name}"

    def is_downloaded(self, config: Config) -> bool:
        return (self.local_dir(config) / DISK_MODEL_FILE).is_file()

    # -- model download -------------------------------------------------- #

    def download(self, config: Config) -> None:
        """Pre-fetch the converted CTranslate2 model into ``models/whisper``."""
        from huggingface_hub import snapshot_download

        model = config.asr.whisper.model
        target = self.local_dir(config)
        target.mkdir(parents=True, exist_ok=True)
        repo_id = REPO_TEMPLATE.format(model=Path(model).name)
        LOGGER.info("downloading whisper %s -> %s", repo_id, target)
        try:
            snapshot_download(repo_id=repo_id, local_dir=str(target))
        except Exception as exc:
            raise AsrError(
                f"could not download {repo_id}: {exc}. A custom faster-whisper model "
                f"name must be converted locally; see the faster-whisper docs"
            ) from exc

    # -- lifecycle ------------------------------------------------------- #

    def load(self, config: Config) -> None:
        from faster_whisper import WhisperModel

        self.config = config
        settings = config.asr.whisper
        local = self.local_dir(config)

        if self.is_downloaded(config):
            model_ref = str(local)
            download_root = str(local.parent)
        else:
            # Reached only with --download-models; the daemon refuses to start
            # with a missing model instead of downloading silently.
            model_ref = settings.model
            download_root = str(self.model_root(config))

        compute_type = settings.compute_type
        device = config.asr.device
        if device == "cpu" and compute_type == "float16":
            LOGGER.warning("float16 is not supported on CPU; switching whisper to int8")
            compute_type = "int8"

        LOGGER.info(
            "loading whisper model=%s device=%s compute_type=%s",
            model_ref,
            device,
            compute_type,
        )
        try:
            self._model = WhisperModel(
                model_ref,
                device=device,
                compute_type=compute_type,
                download_root=download_root,
                num_workers=1,
            )
        except Exception as exc:
            raise AsrError(
                f"could not load whisper ({model_ref}, device={device}, "
                f"compute_type={compute_type}): {exc}"
            ) from exc
        self._loaded = True

    # -- inference ------------------------------------------------------- #

    def transcribe(self, pcm: np.ndarray) -> str:
        if self._model is None:
            raise AsrError("whisper backend is not loaded")
        samples = self.to_pcm(pcm)
        if samples.size == 0:
            return ""
        settings = self.config.asr.whisper if self.config else None
        beam_size = settings.beam_size if settings else 1
        condition = settings.condition_on_previous_text if settings else False
        language = self.config.asr.language if self.config else "pt"
        try:
            segments, info = self._model.transcribe(
                samples,
                language=language,
                beam_size=beam_size,
                condition_on_previous_text=condition,
                vad_filter=False,  # the pipeline already segmented this audio
                word_timestamps=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise AsrError(f"whisper transcription failed: {exc}") from exc
        self.last_language = getattr(info, "language", None) or language
        return text
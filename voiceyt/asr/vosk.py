"""Vosk offline backend (small en + pt models, plan2.md §4).

Vosk ships one Kaldi model per language: ``asr.language`` selects the model
directory (``en*`` -> english, ``pt*`` -> portuguese), so there is no
auto-detection to drift into a third language.  Each model is ~40-50 MB and
transcribes a VAD utterance in ~0.05-0.15 s on CPU, roughly 10x faster than
faster-whisper small.

The ``vosk`` PyPI wheel bundles its own native library, so no system
libraries or PATH tricks are needed.  An optional ``asr.vosk_grammar`` list
constrains the decoder to those phrases (good for the fixed verbs); leave it
null for free-form song queries.
"""

from __future__ import annotations

import io
import json
import logging
import stat
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np

from ..config import Config
from .base import AsrError, BaseBackend

LOGGER = logging.getLogger(__name__)

MODEL_URL_TEMPLATE = "https://alphacephei.com/vosk/models/{name}.zip"
SAMPLE_RATE = 16000
MAX_MODEL_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MODEL_FILES = 20_000


def _safe_extract_model(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract a model ZIP without traversal, links, or decompression bombs."""
    destination = destination.resolve()
    total = 0
    members = archive.infolist()
    if len(members) > MAX_MODEL_FILES:
        raise ValueError(f"model archive contains too many files ({len(members)})")
    for member in members:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        windows_path = PureWindowsPath(member.filename)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in path.parts
            or not path.parts
        ):
            raise ValueError(f"unsafe archive path {member.filename!r}")
        if stat.S_ISLNK(mode):
            raise ValueError(f"archive links are not allowed: {member.filename!r}")
        target = (destination / Path(*path.parts)).resolve()
        if target != destination and destination not in target.parents:
            raise ValueError(f"archive path escapes destination: {member.filename!r}")
        total += member.file_size
        if total > MAX_MODEL_ARCHIVE_BYTES:
            raise ValueError("model archive expands beyond the 2 GiB safety limit")
    archive.extractall(destination)


class VoskBackend(BaseBackend):
    """Batch Vosk recognizer, one utterance per ``transcribe`` call."""

    name = "vosk"
    streaming = False

    def __init__(self) -> None:
        super().__init__()
        self._recognizer = None
        self._model_name = ""

    # -- model selection ------------------------------------------------- #

    def model_name(self, config: Config) -> str:
        """Pick the configured model directory from ``asr.language``."""
        language = (config.asr.language or "en").lower()
        if language.startswith("en"):
            return config.asr.vosk_model_en
        if language.startswith("pt"):
            return config.asr.vosk_model_pt
        raise AsrError(
            f"vosk has no model for language {config.asr.language!r}; "
            "use an 'en...' or 'pt...' value for asr.language"
        )

    def local_dir(self, config: Config) -> Path:
        return self.model_root(config) / self.model_name(config)

    def is_downloaded(self, config: Config) -> bool:
        try:
            directory = self.local_dir(config)
        except AsrError:
            return False
        return directory.is_dir() and (directory / "am" / "final.mdl").is_file()

    # -- model download -------------------------------------------------- #

    def download(self, config: Config) -> None:
        """Fetch the model zip from alphacephei.com into ``models/vosk``."""
        name = self.model_name(config)
        target = self.model_root(config) / name
        url = MODEL_URL_TEMPLATE.format(name=name)
        LOGGER.info("downloading vosk %s -> %s", url, target)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
                payload = response.read()
        except Exception as exc:
            raise AsrError(f"could not download {url}: {exc}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                _safe_extract_model(archive, target.parent)
        except Exception as exc:
            raise AsrError(f"could not unpack {name}.zip: {exc}") from exc
        if not (target / "am" / "final.mdl").is_file():
            raise AsrError(
                f"{name}.zip unpacked but {target / 'am' / 'final.mdl'} is missing"
            )

    # -- lifecycle ------------------------------------------------------- #

    def load(self, config: Config) -> None:
        from vosk import KaldiRecognizer, Model

        self.config = config
        name = self.model_name(config)
        directory = self.model_root(config) / name
        if not (directory / "am" / "final.mdl").is_file():
            raise AsrError(
                f"vosk model {name} is missing; run "
                "python -m voiceyt --download-models vosk"
            )
        LOGGER.info("loading vosk model=%s path=%s", name, directory)
        try:
            model = Model(str(directory))
            grammar = config.asr.vosk_grammar
            if grammar:
                self._recognizer = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(grammar))
            else:
                self._recognizer = KaldiRecognizer(model, SAMPLE_RATE)
        except Exception as exc:
            raise AsrError(f"could not load vosk model {name}: {exc}") from exc
        self._model_name = name
        self._loaded = True
        self.last_language = config.asr.language

    def close(self) -> None:
        self._recognizer = None
        super().close()

    # -- inference ------------------------------------------------------- #

    def transcribe(self, pcm: np.ndarray) -> str:
        if self._recognizer is None:
            raise AsrError("vosk backend is not loaded")
        samples = self.to_pcm(pcm)
        if samples.size == 0:
            return ""
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        try:
            self._recognizer.Reset()
            self._recognizer.AcceptWaveform(pcm16.tobytes())
            result = json.loads(self._recognizer.FinalResult() or "{}")
        except Exception as exc:
            raise AsrError(f"vosk transcription failed: {exc}") from exc
        return str(result.get("text") or "").strip()

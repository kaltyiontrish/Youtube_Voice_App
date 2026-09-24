"""Vosk backend contract: model selection, download detection, transcription."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from voiceyt.asr.vosk import VoskBackend, _safe_extract_model
from voiceyt.config import load_config

from test_config import write_config


def _config_with(language: str, directory: Path) -> object:
    return load_config(
        write_config(
            directory,
            {
                "asr": {
                    "backend": "vosk",
                    "device": "cpu",
                    "models_dir": str(directory / "models"),
                    "language": language,
                },
                "vad": {"enabled": False},
                "behaviour": {"log_transcripts": False},
            },
        )
    )


class ModelSelectionTests(unittest.TestCase):
    def test_language_selects_model_directory(self) -> None:
        backend = VoskBackend()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertTrue(
                backend.model_name(_config_with("en", directory)).startswith(
                    "vosk-model-small-en"
                )
            )
            self.assertTrue(
                backend.model_name(_config_with("pt-PT", directory)).startswith(
                    "vosk-model-small-pt"
                )
            )
            with self.assertRaises(Exception):
                backend.model_name(_config_with("ru", directory))

    def test_missing_model_is_not_downloaded(self) -> None:
        backend = VoskBackend()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertFalse(backend.is_downloaded(_config_with("en", directory)))


class ModelArchiveTests(unittest.TestCase):
    def _archive(self, name: str) -> zipfile.ZipFile:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(name, "bad")
        payload.seek(0)
        return zipfile.ZipFile(payload)

    def test_traversal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            with self._archive("../escaped") as archive:
                with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                    _safe_extract_model(archive, destination)
            self.assertFalse((destination.parent / "escaped").exists())

    def test_windows_traversal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self._archive(r"..\escaped") as archive:
                with self.assertRaises(ValueError):
                    _safe_extract_model(archive, Path(temp))

    def test_windows_drive_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self._archive("C:/escaped") as archive:
                with self.assertRaises(ValueError):
                    _safe_extract_model(archive, Path(temp))



    def test_oversized_expansion_is_rejected(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large", b"0" * 1_000_000)
        payload.seek(0)
        with tempfile.TemporaryDirectory() as temp:
            with zipfile.ZipFile(payload) as archive:
                member = archive.infolist()[0]
                original = member.file_size
                member.file_size = 3 * 1024 * 1024 * 1024
                with self.assertRaisesRegex(ValueError, "2 GiB"):
                    _safe_extract_model(archive, Path(temp))
                member.file_size = original





class TranscribeTests(unittest.TestCase):
    def test_transcribe_returns_recognizer_text(self) -> None:
        backend = VoskBackend()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config = _config_with("en", directory)

            class FakeRecognizer:
                def Reset(self) -> None:
                    pass

                def AcceptWaveform(self, data: bytes) -> bool:
                    assert data
                    return True

                def FinalResult(self) -> str:
                    return json.dumps({"text": "youtube play nirvana"})

            class FakeModel:
                def __init__(self, path: str) -> None:
                    self.path = path

            (backend.local_dir(config) / "am").mkdir(parents=True)
            (backend.local_dir(config) / "am" / "final.mdl").write_bytes(b"fake")
            with patch("vosk.Model", FakeModel), patch(
                "vosk.KaldiRecognizer", lambda *args: FakeRecognizer()
            ):
                backend.load(config)
            text = backend.transcribe(np.zeros(16000, dtype=np.float32) + 0.1)
            self.assertEqual(text, "youtube play nirvana")
            self.assertEqual(backend.transcribe(np.zeros(0, dtype=np.float32)), "")


if __name__ == "__main__":
    unittest.main()

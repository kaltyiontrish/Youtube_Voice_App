"""Vosk backend contract: model selection, download detection, transcription."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from voiceyt.asr.vosk import VoskBackend
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

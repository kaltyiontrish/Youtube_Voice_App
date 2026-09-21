"""Configuration loading and validation (plan2.md §10).

Each test writes its own config.yaml into a temporary directory, so the real
config.yaml is never modified and the tests do not depend on its values.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from voiceyt.config import ConfigError, DEFAULTS, load_config

MINIMAL = {
    "asr": {"backend": "whisper", "device": "cpu", "models_dir": "./models"},
    "vad": {"enabled": False},
    "behaviour": {"log_transcripts": False},
}


def write_config(directory: Path, payload: dict) -> Path:
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


class LoadingTests(unittest.TestCase):
    def test_defaults_fill_in_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(write_config(Path(temp), MINIMAL))
        self.assertEqual(config.audio.sample_rate, 16000)
        self.assertEqual(config.trigger.words[0], "youtube")
        self.assertEqual(config.trigger.trigger_window_ms, 1500)
        self.assertEqual(config.asr.backend, "whisper")
        self.assertEqual(len(config.commands), len(DEFAULTS["commands"]))

    def test_paths_are_resolved_against_the_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config = load_config(write_config(directory, MINIMAL))
        self.assertTrue(config.asr.models_dir.is_absolute())
        self.assertEqual(config.asr.models_dir, (directory / "models").resolve())
        self.assertTrue(config.behaviour.log_path.is_absolute())

    def test_backend_override_is_applied_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(write_config(Path(temp), MINIMAL), backend="nemotron")
        self.assertEqual(config.asr.backend, "nemotron")

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(Path("definitely-not-here.yaml"))

    def test_broken_yaml_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            path.write_text("audio: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_whisper_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(write_config(Path(temp), MINIMAL))
        self.assertEqual(config.asr.whisper.model, "small")
        self.assertEqual(config.asr.whisper.beam_size, 1)
        self.assertFalse(config.asr.whisper.condition_on_previous_text)

    def test_vosk_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(write_config(Path(temp), MINIMAL))
        self.assertEqual(config.asr.vosk_model_en, "vosk-model-small-en-us-0.15")
        self.assertEqual(config.asr.vosk_model_pt, "vosk-model-small-pt-0.3")
        self.assertIsNone(config.asr.vosk_grammar)

    def test_verbs_are_lowercased(self) -> None:
        payload = dict(MINIMAL)
        payload["commands"] = [{"action": "stop", "verbs": ["PARA"], "takes_query": False}]
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(write_config(Path(temp), payload))
        self.assertEqual(config.commands[0].verbs, ("para",))

    def test_shipped_config_is_valid(self) -> None:
        """The documented default must always load."""
        repo_root = Path(__file__).resolve().parent.parent
        config = load_config(repo_root / "config.yaml")
        self.assertIn(config.asr.backend, ("whisper", "parakeet", "nemotron", "vosk"))


class ValidationTests(unittest.TestCase):
    def load_with(self, payload: dict) -> Config:
        with tempfile.TemporaryDirectory() as temp:
            return load_config(write_config(Path(temp), payload))

    def test_vosk_grammar_must_be_strings_or_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(
                write_config(Path(temp), {"asr": {"vosk": {"grammar": ["youtube play"]}}})
            )
        self.assertEqual(config.asr.vosk_grammar, ("youtube play",))
        with self.assertRaises(ConfigError):
            self.load_with({"asr": {"vosk": {"grammar": ["ok", 42]}}})

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"asr": {"backend": "whisperr"}})

    def test_wrong_sample_rate_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"audio": {"sample_rate": 44100}})

    def test_unknown_aec_backend_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"aec": {"enabled": True, "backend": "magic"}})

    def test_bad_nemotron_chunk_size_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"asr": {"nemotron": {"chunk_ms": 500}}})

    def test_volume_range_must_be_sane(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"player": {"volume_min": 50, "volume_max": 10}})

    def test_threshold_must_be_a_probability(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"vad": {"threshold": 1.5}})

    def test_empty_trigger_words_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"trigger": {"words": []}})

    def test_duplicate_verb_is_rejected(self) -> None:
        payload = {
            "commands": [
                {"action": "play", "verbs": ["passa"], "takes_query": True},
                {"action": "toca", "verbs": ["passa"], "takes_query": False},
            ]
        }
        with self.assertRaises(ConfigError):
            self.load_with(payload)

    def test_commands_must_not_be_empty(self) -> None:
        with self.assertRaises(ConfigError):
            self.load_with({"commands": []})

    def test_unknown_action_is_a_startup_error(self) -> None:
        from voiceyt.commands import validate_actions

        payload = {
            "commands": [
                {"action": "teleport", "verbs": ["teletransporta"], "takes_query": False}
            ]
        }
        config = self.load_with(payload)
        with self.assertRaises(ConfigError):
            validate_actions(config)

    def test_known_actions_cover_the_plan(self) -> None:
        from voiceyt.commands import known_actions

        self.assertEqual(
            sorted(known_actions()),
            ["next", "play", "prev", "resume", "stop", "volume_down", "volume_up"],
        )

    def test_shipped_config_has_known_actions(self) -> None:
        from voiceyt.commands import validate_actions

        repo_root = Path(__file__).resolve().parent.parent
        validate_actions(load_config(repo_root / "config.yaml"))


if __name__ == "__main__":
    unittest.main()
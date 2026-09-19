"""Journal replay and the M3 acceptance test (plan2.md §9, M3).

``--replay-log`` is the only practical way to measure the false-trigger rate
without sitting in front of the microphone for hours, so these tests cover the
journal format and the replay driver.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from voiceyt.config import Config, CommandSpec, load_config
from voiceyt.transcripts import (
    COMMAND,
    FINAL,
    PARTIAL,
    LogEntry,
    TranscriptLog,
    read_entries,
    replay,
)

COMMANDS = (
    CommandSpec(action="play", verbs=("passa", "toca"), takes_query=True),
    CommandSpec(action="next", verbs=("proximo",), takes_query=False),
    CommandSpec(action="stop", verbs=("para",), takes_query=False),
    CommandSpec(action="volume_up", verbs=("mais alto",), takes_query=False),
    CommandSpec(action="volume_down", verbs=("mais baixo",), takes_query=False),
)

BASE = datetime(2026, 9, 19, 18, 0, 0)


def config_for(directory: Path) -> Config:
    """A real Config built through the normal loader; replay needs trigger/commands."""
    payload = {
        "asr": {"backend": "whisper", "device": "cpu", "models_dir": "./models"},
        "vad": {"enabled": False},
        "behaviour": {"log_transcripts": False},
        "commands": [
            {
                "action": spec.action,
                "verbs": list(spec.verbs),
                "takes_query": spec.takes_query,
            }
            for spec in COMMANDS
        ],
    }
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_config(path)


def journal(rows: list[tuple[float, str, str]]) -> list[LogEntry]:
    return [
        LogEntry(ts=BASE.timestamp() + offset, backend="test", tag=tag, text=text)
        for offset, tag, text in rows
    ]


class JournalFormatTests(unittest.TestCase):
    def test_write_and_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "transcripts.log"
            with TranscriptLog(path, enabled=True) as log:
                log.write("youtube passa nirvana", "parakeet")
                log.write("youtube passa nir", "nemotron", tag=PARTIAL)
                log.note("action=play query=nirvana")
            entries = list(read_entries(path))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].tag, FINAL)
        self.assertEqual(entries[0].text, "youtube passa nirvana")
        self.assertEqual(entries[1].tag, PARTIAL)
        self.assertFalse(entries[1].feeds_matcher)
        self.assertEqual(entries[2].tag, COMMAND)

    def test_whitespace_is_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transcripts.log"
            with TranscriptLog(path, enabled=True) as log:
                log.write("  youtube   passa\t nirvana ", "whisper")
            text = list(read_entries(path))[0].text
        self.assertEqual(text, "youtube passa nirvana")

    def test_disabled_log_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transcripts.log"
            with TranscriptLog(path, enabled=False) as log:
                log.write("ignored", "whisper")
            self.assertFalse(path.exists())

    def test_malformed_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "transcripts.log"
            good = (BASE + timedelta(seconds=1)).isoformat(timespec="milliseconds")
            path.write_text(
                f"nonsense without tabs\n{good}\twhisper\tfinal\tyoutube passa x\n",
                encoding="utf-8",
            )
            entries = list(read_entries(path))
        self.assertEqual([entry.text for entry in entries], ["youtube passa x"])


class ReplayTests(unittest.TestCase):
    def test_ordinary_conversation_fires_nothing(self) -> None:
        """The M3 acceptance criterion."""
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal(
            [
                (0.0, FINAL, "vi um video no youtube ontem"),
                (3.0, FINAL, "o youtube tem coisas boas"),
                (7.0, FINAL, "para mim esta bem"),
                (12.0, FINAL, "isso esta mais alto do que devia"),
                (20.0, FINAL, "proximo domingo vou a lisboa"),
            ]
        )
        firings, _ = replay(config, entries)
        self.assertEqual(firings, [])

    def test_commands_do_fire(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal(
            [
                (0.0, FINAL, "youtube passa nirvana"),
                (5.0, FINAL, "youtube proximo"),
                (9.0, FINAL, "youtube mais alto"),
                (12.0, FINAL, "youtube para"),
            ]
        )
        firings, _ = replay(config, entries)
        self.assertEqual(
            [command.action for _, command in firings],
            ["play", "next", "volume_up", "stop"],
        )
        self.assertEqual(firings[0][1].query, "nirvana")

    def test_partials_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal(
            [
                (0.0, PARTIAL, "youtube"),
                (0.4, PARTIAL, "youtube passa"),
                (0.8, PARTIAL, "youtube passa nirvana"),
                (1.2, FINAL, "youtube passa nirvana"),
            ]
        )
        firings, _ = replay(config, entries)
        self.assertEqual(len(firings), 1)
        self.assertEqual(firings[0][1].query, "nirvana")

    def test_playing_state_is_reconstructed_from_notes(self) -> None:
        """After action=play, a split trigger/verb must be blocked (§8 step 3)."""
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal(
            [
                (0.0, FINAL, "youtube passa a"),
                (1.0, COMMAND, "action=play query=a"),
                (6.0, FINAL, "youtube"),
                (6.4, FINAL, "passa b"),
            ]
        )
        firings, _ = replay(config, entries)
        self.assertEqual([command.query for _, command in firings], ["a"])

    def test_long_pause_ends_the_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal(
            [
                (0.0, FINAL, "youtube passa"),
                (3.0, FINAL, "algo completamente diferente"),
            ]
        )
        firings, transitions = replay(config, entries)
        # The 3 s gap ends the empty query rather than starting a new command.
        self.assertEqual(firings, [])
        self.assertTrue(any("fire:" in line for line in transitions))

    def test_transitions_are_reported_for_debugging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
        entries = journal([(0.0, FINAL, "youtube passa x")])
        _, transitions = replay(config, entries)
        self.assertTrue(any("IDLE -> ARMED" in line for line in transitions))
        self.assertTrue(any("fire:" in line for line in transitions))


if __name__ == "__main__":
    unittest.main()
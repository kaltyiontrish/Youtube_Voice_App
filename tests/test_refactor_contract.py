"""W0 characterization tests for public behavior before refactoring."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from voiceyt.ui import UiState


class RefactorContractTests(unittest.TestCase):
    def test_cli_accepts_current_modes_and_overrides(self) -> None:
        from voiceyt.__main__ import build_parser

        parser = build_parser()
        for argv in (
            ["--list-devices"], ["--meter"], ["--aec-probe"], ["--listen"],
            ["--backend", "whisper"], ["--config", "config.yaml"],
            ["--log-level", "DEBUG"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(parser.parse_args(argv))

    def test_current_command_actions_are_registered(self) -> None:
        from voiceyt.commands import known_actions

        self.assertEqual(
            sorted(known_actions()),
            ["add", "jump", "next", "pause", "play", "prev", "remove",
             "resume", "stop", "volume_down", "volume_up"],
        )

    def test_browse_query_is_not_rewritten(self) -> None:
        from voiceyt.search import Searcher

        captured = {}

        class FakeYDL:
            def __init__(self, options):
                captured["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, term, download=False):
                captured["term"] = term
                return {"entries": []}

        with patch("voiceyt.search.yt_dlp.YoutubeDL", FakeYDL):
            self.assertEqual(Searcher(results=10).search("ornatos violeta", music_only=True), [])
        self.assertEqual(captured["term"], "ytsearch10:ornatos violeta")

    def test_ui_snapshot_exposes_current_state_keys(self) -> None:
        state = UiState()
        snapshot = state.snapshot()
        for key in (
            "heard", "action", "error", "volume", "playing", "voice_previews",
            "voice_preview_urls", "recent", "search_results", "browse_query",
            "voice_query",
        ):
            self.assertIn(key, snapshot)
        snapshot["heard"] = "copy"
        self.assertEqual(state.snapshot()["heard"], "")


if __name__ == "__main__":
    unittest.main()

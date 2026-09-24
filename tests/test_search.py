"""Search query construction and result parsing tests."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from voiceyt.search import Searcher


class SearchQueryTests(unittest.TestCase):
    def test_music_search_keeps_artist_query_unchanged(self) -> None:
        captured = {}

        class FakeYDL:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, term, download=False):
                captured["term"] = term
                return {"entries": [{"id": "abc", "title": "Song", "url": "https://youtube.com/watch?v=abc"}]}

        with patch("voiceyt.search.yt_dlp.YoutubeDL", FakeYDL):
            results = Searcher(results=10).search("ornatos violeta", music_only=True)
        self.assertEqual(captured["term"], "ytsearch10:ornatos violeta")
        self.assertEqual(results[0].title, "Song")


if __name__ == "__main__":
    unittest.main()

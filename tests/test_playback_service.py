"""Tests for the playback coordination boundary."""

from __future__ import annotations

import unittest

from voiceyt.playback_service import PlaybackService
from voiceyt.search import SearchResult


class PlaybackServiceTests(unittest.TestCase):
    def test_search_and_play_delegates_to_player_and_searcher(self) -> None:
        calls: list[tuple] = []

        class Player:
            alive = True
            def play_results(self, results, searcher, start_index=0):
                calls.append(("play", tuple(results), start_index))

        class Searcher:
            def search(self, query):
                calls.append(("search", query))
                return [SearchResult("abc123", "Track", "https://youtube.com/watch?v=abc123")]

        results = PlaybackService(Player(), Searcher()).search_and_play("artist")
        self.assertEqual(results[0].title, "Track")
        self.assertEqual(calls, [
            ("search", "artist"),
            ("play", tuple(results), 0),
        ])

    def test_empty_search_does_not_call_player(self) -> None:
        calls: list[tuple] = []

        class Player:
            alive = True
            def play_results(self, *_args, **_kwargs):
                calls.append(("play",))

        class Searcher:
            def search(self, _query):
                return []

        self.assertEqual(PlaybackService(Player(), Searcher()).search_and_play("x"), ())
        self.assertEqual(calls, [])

    def test_snapshot_reports_shared_playback_state(self) -> None:
        class Player:
            alive = True
            def current_position(self): return 2
            def current_title(self): return "Track"
            def current_url(self): return "https://youtube.com/watch?v=abc123"
            def playback_position(self): return (12.5, 180.0)
            def is_playing(self): return True

        snapshot = PlaybackService(Player(), object()).snapshot()
        self.assertEqual(snapshot.current_index, 2)
        self.assertEqual(snapshot.current_url, "https://youtube.com/watch?v=abc123")
        self.assertEqual(snapshot.position, 12.5)
        self.assertEqual(snapshot.duration, 180.0)
        self.assertTrue(snapshot.state.value == "playing")

    def test_transport_methods_serialize_player_calls(self) -> None:
        calls: list[str] = []
        class Player:
            alive = True
            def next_track(self): calls.append("next"); return True
            def prev_track(self): calls.append("prev"); return True
            def stop(self): calls.append("stop"); return True
            def pause(self): calls.append("pause"); return True
            def unpause(self): calls.append("resume"); return True
            def set_volume(self, value): calls.append("volume"); return value

        service = PlaybackService(Player(), object())
        self.assertTrue(service.next_track())
        self.assertTrue(service.prev_track())
        self.assertTrue(service.stop())
        self.assertTrue(service.pause())
        self.assertTrue(service.unpause())
        self.assertEqual(service.set_volume(65), 65)
        self.assertEqual(calls, ["next", "prev", "stop", "pause", "resume", "volume"])

    def test_playlist_rows_skip_entries_without_urls(self) -> None:
        results = PlaybackService.results_from_playlist([
            {"title": "missing"},
            {"title": "Track", "url": "https://youtube.com/watch?v=abc123", "duration": 3.0},
        ])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].video_id, "abc123")
        self.assertEqual(results[0].duration, 3.0)

    def test_playlist_start_index_is_validated(self) -> None:
        class Player:
            alive = True
            def play_results(self, *_args, **_kwargs):
                raise AssertionError("must not reach player")

        with self.assertRaises(Exception):
            PlaybackService(Player(), object()).play_results([], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

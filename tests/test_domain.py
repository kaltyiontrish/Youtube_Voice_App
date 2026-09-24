"""W1 domain model tests; these must not require external services."""

from __future__ import annotations

import unittest

from voiceyt.domain import (
    DomainError,
    PlaybackEvent,
    PlaybackRequest,
    PlaybackState,
    Track,
    TrackView,
    UiSnapshot,
    normalize_youtube_url,
)


class DomainTests(unittest.TestCase):
    def test_url_variants_normalize_to_one_identity(self) -> None:
        expected = "https://www.youtube.com/watch?v=abc123"
        for value in (
            "https://www.youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://www.youtube.com/shorts/abc123",
        ):
            self.assertEqual(normalize_youtube_url(value), ("abc123", expected))

    def test_track_validates_identity_and_bounds(self) -> None:
        track = Track("abc123", "Song", "https://youtu.be/abc123", duration=12.5)
        self.assertEqual(track.canonical_url, "https://www.youtube.com/watch?v=abc123")
        with self.assertRaises(DomainError):
            Track("wrong", "Song", "https://youtu.be/abc123")
        with self.assertRaises(DomainError):
            Track("abc123", "", "https://youtu.be/abc123")
        with self.assertRaises(DomainError):
            Track("abc123", "Song", "https://youtu.be/abc123", duration=0)

    def test_request_and_event_validate_indices(self) -> None:
        track = Track("abc123", "Song", "https://youtu.be/abc123")
        request = PlaybackRequest((track,), source="voice")
        self.assertEqual(request.start_index, 0)
        with self.assertRaises(DomainError):
            PlaybackRequest((track,), start_index=1)
        event = PlaybackEvent(0, 1, PlaybackState.PLAYING, current_index=0)
        self.assertEqual(event.state, PlaybackState.PLAYING)
        with self.assertRaises(DomainError):
            PlaybackEvent(0, 1, PlaybackState.PLAYING, position=-1)

    def test_snapshot_is_immutable_and_validates_display_state(self) -> None:
        track = Track("abc123", "Song", "https://youtu.be/abc123")
        snapshot = UiSnapshot(queue=(TrackView(track, current=True),), volume=65)
        self.assertTrue(snapshot.queue[0].current)
        with self.assertRaises(DomainError):
            UiSnapshot(volume=999)
        with self.assertRaises(DomainError):
            UiSnapshot(mic_level=2.0)
        with self.assertRaises(Exception):
            snapshot.volume = 1


if __name__ == "__main__":
    unittest.main()

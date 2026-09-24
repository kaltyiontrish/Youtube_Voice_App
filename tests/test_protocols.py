"""W2 protocol seam tests; no production caller is migrated here."""

from __future__ import annotations

import unittest

from tests.fakes import FakePlayer, FakePlaylist, FakeSearcher
from voiceyt.domain import Track
from voiceyt.protocols import PlayerPort, PlaylistPort, SearchPort


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.track = Track("abc123", "Song", "https://youtu.be/abc123")

    def test_fakes_satisfy_protocols(self) -> None:
        self.assertIsInstance(FakeSearcher((self.track,)), SearchPort)
        self.assertIsInstance(FakePlayer(), PlayerPort)
        self.assertIsInstance(FakePlaylist(), PlaylistPort)

    def test_fakes_record_calls_without_services(self) -> None:
        searcher = FakeSearcher((self.track,))
        self.assertEqual(searcher.search("artist", True), (self.track,))
        self.assertEqual(searcher.calls, [("artist", True)])

        player = FakePlayer()
        player.play_queue((self.track,), 0)
        player.set_volume(65)
        self.assertEqual(player.volume(), 65)
        self.assertEqual(player.calls[0][0], "play_queue")
        self.assertEqual(player.calls[1], ("volume", 65))

    def test_fake_playlist_crud_is_deterministic(self) -> None:
        store = FakePlaylist()
        store.create("Favorites")
        store.add("Favorites", "Song", self.track.canonical_url)
        self.assertEqual(store.names(), ["Favorites"])
        self.assertEqual(store.tracks("Favorites")[0]["title"], "Song")
        self.assertEqual(store.calls, [("create", "Favorites"), ("add", "Favorites")])


if __name__ == "__main__":
    unittest.main()

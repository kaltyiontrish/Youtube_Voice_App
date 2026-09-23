"""PlaylistStore: round-trip, atomic save and corrupt-file recovery (C1/C4)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voiceyt.playlists import PlaylistStore


class PlaylistStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "playlists.json"

    def test_missing_file_starts_empty(self) -> None:
        store = PlaylistStore(self.path)
        self.assertEqual(store.names(), [])

    def test_round_trip_create_add_reorder(self) -> None:
        store = PlaylistStore(self.path)
        store.create("chill")
        store.add("chill", "Song A", "https://a")
        store.add("chill", "Song B", "https://b")
        reloaded = PlaylistStore(self.path)
        self.assertEqual(reloaded.names(), ["chill"])
        tracks = reloaded.tracks("chill")
        self.assertEqual([t["title"] for t in tracks], ["Song A", "Song B"])
        reloaded.reorder("chill", 0, 1)
        self.assertEqual(
            [t["title"] for t in PlaylistStore(self.path).tracks("chill")],
            ["Song B", "Song A"],
        )

    def test_rename_and_delete_persist(self) -> None:
        store = PlaylistStore(self.path)
        store.create("old")
        store.rename("old", "new")
        store.delete("new")
        self.assertEqual(PlaylistStore(self.path).names(), [])

    def test_remove_track_by_index(self) -> None:
        store = PlaylistStore(self.path)
        store.create("p")
        store.add("p", "A", "https://a")
        store.add("p", "B", "https://b")
        store.remove("p", 0)
        self.assertEqual([t["title"] for t in store.tracks("p")], ["B"])

    def test_duplicate_and_unknown_names_are_rejected(self) -> None:
        store = PlaylistStore(self.path)
        store.create("p")
        with self.assertRaises(ValueError):
            store.create("p")
        with self.assertRaises(ValueError):
            store.create("   ")
        with self.assertRaises(ValueError):
            store.add("missing", "t", "https://x")
        with self.assertRaises(ValueError):
            store.delete("missing")

    def test_bad_indices_are_rejected(self) -> None:
        store = PlaylistStore(self.path)
        store.create("p")
        store.add("p", "A", "https://a")
        with self.assertRaises(IndexError):
            store.remove("p", 5)
        with self.assertRaises(IndexError):
            store.reorder("p", 0, 9)

    def test_corrupt_file_backs_up_and_starts_empty(self) -> None:
        self.path.write_text("{not json at all", encoding="utf-8")
        store = PlaylistStore(self.path)
        self.assertEqual(store.names(), [])
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "{not json at all")
        store.create("fresh")          # saving over the wreckage works
        self.assertEqual(PlaylistStore(self.path).names(), ["fresh"])

    def test_save_is_atomic_no_tmp_left_behind(self) -> None:
        store = PlaylistStore(self.path)
        store.create("p")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("playlists", payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

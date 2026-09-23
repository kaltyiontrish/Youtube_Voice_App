"""Saved playlists stored as JSON at ``player.playlist_path`` (UI_Design.MD §C).

One file, one job: ``{"playlists": {"name": [{"title": ..., "url": ...}]}}``.
Stdlib only, atomic saves (write-temp-then-replace), and a corrupt file is
backed up to ``*.bak`` and restarted empty - the daemon must never die because
a hand-edited playlist went wrong.  Playlists are UI-only until a later
milestone wires new voice actions through ``commands.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)

Track = dict  # {"title": str, "url": str} - plain dicts keep JSON round-trips trivial


class PlaylistStore:
    """Load/mutate/save the playlist file; every mutation persists at once."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._playlists: dict[str, list[Track]] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            playlists = raw.get("playlists", {}) if isinstance(raw, dict) else {}
            if not isinstance(playlists, dict):
                raise ValueError("playlists must be an object")
            cleaned: dict[str, list[Track]] = {}
            for name, tracks in playlists.items():
                if not isinstance(tracks, list):
                    raise ValueError(f"playlist {name!r} must be a list")
                cleaned[str(name)] = [
                    {"title": str(t.get("title", "")), "url": str(t.get("url", ""))}
                    for t in tracks
                    if isinstance(t, dict) and t.get("url")
                ]
            self._playlists = cleaned
        except Exception as exc:  # never crash the daemon over a data file
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                os.replace(self.path, backup)
                LOGGER.warning("corrupt playlists at %s moved to %s: %s",
                               self.path, backup, exc)
            except OSError:
                LOGGER.warning("corrupt playlists at %s ignored: %s", self.path, exc)
            self._playlists = {}

    def save(self) -> None:
        """Atomic: write a temp file in the same directory, then replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"playlists": self._playlists}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # -- reads -------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._playlists)

    def tracks(self, name: str) -> list[Track]:
        return [dict(t) for t in self._playlists.get(name, [])]

    # -- mutations (each saves immediately) --------------------------------

    def create(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("playlist name cannot be empty")
        if name in self._playlists:
            raise ValueError(f"playlist {name!r} already exists")
        self._playlists[name] = []
        self.save()

    def rename(self, old: str, new: str) -> None:
        new = new.strip()
        if not new:
            raise ValueError("playlist name cannot be empty")
        if old not in self._playlists:
            raise ValueError(f"playlist {old!r} does not exist")
        if new in self._playlists:
            raise ValueError(f"playlist {new!r} already exists")
        self._playlists[new] = self._playlists.pop(old)
        self.save()

    def delete(self, name: str) -> None:
        if name not in self._playlists:
            raise ValueError(f"playlist {name!r} does not exist")
        del self._playlists[name]
        self.save()

    def add(self, name: str, title: str, url: str) -> None:
        if name not in self._playlists:
            raise ValueError(f"playlist {name!r} does not exist")
        if not url:
            raise ValueError("track url cannot be empty")
        self._playlists[name].append({"title": title or url, "url": url})
        self.save()

    def remove(self, name: str, index: int) -> None:
        tracks = self._tracks_checked(name, index)
        tracks.pop(index)
        self.save()

    def reorder(self, name: str, index: int, new_index: int) -> None:
        tracks = self._tracks_checked(name, index)
        if not 0 <= new_index < len(tracks):
            raise IndexError(f"target index {new_index} out of range")
        tracks.insert(new_index, tracks.pop(index))
        self.save()

    def _tracks_checked(self, name: str, index: int) -> list[Track]:
        if name not in self._playlists:
            raise ValueError(f"playlist {name!r} does not exist")
        tracks = self._playlists[name]
        if not 0 <= index < len(tracks):
            raise IndexError(f"track index {index} out of range")
        return tracks

"""Deterministic fakes for refactor contract tests; not production code."""

from __future__ import annotations

from collections.abc import Mapping

from voiceyt.domain import PlaybackEvent, PlaybackState, Track
from voiceyt.protocols import PlayerPort, PlaylistPort, SearchPort


class FakeSearcher:
    def __init__(self, tracks: tuple[Track, ...] = ()) -> None:
        self.tracks = tracks
        self.calls: list[tuple[str, bool]] = []

    def search(self, query: str, music_only: bool = False) -> tuple[Track, ...]:
        self.calls.append((query, music_only))
        return self.tracks

    def resolve(self, track: Track) -> tuple[str, Mapping[str, str]]:
        return (f"https://stream.invalid/{track.video_id}", {})


class FakePlayer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.volume_value = 50.0
        self.index = 0
        self.state = PlaybackState.IDLE

    def play_queue(self, tracks: tuple[Track, ...], start_index: int = 0) -> None:
        self.calls.append(("play_queue", tracks, start_index))
        self.index = start_index
        self.state = PlaybackState.PLAYING

    def next_track(self) -> bool:
        self.calls.append(("next",)); self.index += 1; return True

    def prev_track(self) -> bool:
        self.calls.append(("prev",)); self.index = max(0, self.index - 1); return True

    def jump_to(self, index: int) -> bool:
        self.calls.append(("jump", index)); self.index = index; return True

    def pause(self) -> bool:
        self.calls.append(("pause",)); self.state = PlaybackState.PAUSED; return True

    def unpause(self) -> bool:
        self.calls.append(("unpause",)); self.state = PlaybackState.PLAYING; return True

    def stop(self) -> bool:
        self.calls.append(("stop",)); self.state = PlaybackState.STOPPED; return True

    def seek(self, seconds: float) -> bool:
        self.calls.append(("seek", seconds)); return True

    def volume(self) -> float:
        return self.volume_value

    def set_volume(self, value: float) -> float:
        self.calls.append(("volume", value)); self.volume_value = value; return value

    def snapshot(self) -> PlaybackEvent:
        return PlaybackEvent(0, 0, self.state, self.index)


class FakePlaylist:
    def __init__(self) -> None:
        self._items: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []

    def names(self) -> list[str]: return sorted(self._items)
    def tracks(self, name: str) -> list[dict]: return [dict(x) for x in self._items[name]]
    def create(self, name: str) -> None: self.calls.append(("create", name)); self._items[name] = []
    def rename(self, old: str, new: str) -> None: self.calls.append(("rename", old, new)); self._items[new] = self._items.pop(old)
    def delete(self, name: str) -> None: self.calls.append(("delete", name)); del self._items[name]
    def add(self, name: str, title: str, url: str) -> None: self.calls.append(("add", name)); self._items[name].append({"title": title, "url": url})
    def remove(self, name: str, index: int) -> None: self.calls.append(("remove", name, index)); self._items[name].pop(index)
    def reorder(self, name: str, index: int, new_index: int) -> None: self.calls.append(("reorder", name, index, new_index)); self._items[name].insert(new_index, self._items[name].pop(index))

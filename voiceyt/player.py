"""Persistent mpv process driven over JSON IPC (plan2.md §7).

One mpv instance is spawned at daemon start with ``--idle=yes`` and an
``--input-ipc-server`` pipe; it stays alive between commands and is restarted
only if the pipe dies.  Commands and responses are JSON objects, one per line,
matched by ``request_id``; a reader thread keeps the pipe drained so events
(end-file, idle) update the playing state without blocking the caller.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .search import ResolvedStream, SearchError, SearchResult, Searcher

LOGGER = logging.getLogger(__name__)


class PlayerError(RuntimeError):
    """Raised when mpv cannot be started or stops answering."""


def default_ipc_path() -> str:
    """Named pipe on Windows, unix socket elsewhere (plan2.md §7)."""
    if sys.platform.startswith("win"):
        return r"\\.\pipe\voiceyt"
    return "/tmp/voiceyt.sock"


@dataclass(frozen=True)
class MpvStatus:
    playing: bool
    playlist_pos: int | None
    volume: float | None
    title: str | None


class MpvPlayer:
    """Thread-safe wrapper around exactly one mpv process."""

    def __init__(
        self,
        *,
        mpv_path: str = "mpv",
        ipc_path: str | None = None,
        audio_only: bool = True,
        extra_args: Sequence[str] = (),
        volume_min: int = 0,
        volume_max: int = 130,
        volume_step: int = 10,
        start_timeout_s: float = 10.0,
    ) -> None:
        self.mpv_path = mpv_path
        self.ipc_path = ipc_path or default_ipc_path()
        self.audio_only = audio_only
        self.extra_args = tuple(extra_args)
        self.volume_min = int(volume_min)
        self.volume_max = int(volume_max)
        self.volume_step = int(volume_step)
        self.start_timeout_s = float(start_timeout_s)

        self._process: subprocess.Popen[bytes] | None = None
        self._pipe: Any = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._playing = False
        self._queue_generation = 0
        self._stopping = threading.Event()

    # -- lifecycle ------------------------------------------------------- #

    def mpv_binary(self) -> str:
        """Resolve the mpv executable, including a bare name on PATH."""
        resolved = shutil.which(self.mpv_path)
        if resolved:
            return resolved
        candidate = Path(self.mpv_path)
        if candidate.is_file():
            return str(candidate)
        raise PlayerError(
            f"mpv executable {self.mpv_path!r} not found; install mpv and put it on PATH "
            f"(or set player.mpv_path in config.yaml)"
        )

    def start(self) -> "MpvPlayer":
        """Spawn mpv and wait until the IPC pipe answers."""
        binary = self.mpv_binary()
        if not sys.platform.startswith("win"):
            stale = Path(self.ipc_path)
            if stale.exists():
                try:
                    stale.unlink()
                except OSError:  # pragma: no cover
                    pass

        args = [binary, f"--input-ipc-server={self.ipc_path}", "--idle=yes"]
        if self.audio_only:
            args.append("--no-video")
        args.extend(self.extra_args)
        # Let mpv's own ytdl hook find the yt-dlp installed next to the
        # interpreter that runs this daemon.
        env = dict(os.environ)
        script_dir = str(Path(sys.executable).parent)
        env["PATH"] = script_dir + os.pathsep + env.get("PATH", "")
        ytdlp = Path(script_dir) / ("yt-dlp.exe" if sys.platform.startswith("win") else "yt-dlp")
        if ytdlp.is_file():
            args.append(f"--script-opts=ytdl_hook-ytdl_path={ytdlp}")

        LOGGER.debug("starting mpv: %s", " ".join(args))
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        self._process = subprocess.Popen(  # noqa: S603 - the path comes from config
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creation,
        )
        self._stopping.clear()
        self._wait_for_pipe()
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True)
        self._reader.start()
        return self

    def _wait_for_pipe(self) -> None:
        deadline = time.monotonic() + self.start_timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise PlayerError(
                    f"mpv exited immediately with code {self._process.returncode}; "
                    f"check player.mpv_path and player.extra_args"
                )
            try:
                self._pipe = open(self.ipc_path, "r+b", buffering=0)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        raise PlayerError(
            f"mpv did not create its IPC pipe {self.ipc_path!r} within "
            f"{self.start_timeout_s:.0f}s ({last_error})"
        )

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._pipe is not None

    def close(self) -> None:
        """Ask mpv to quit and make sure the process is gone."""
        self._stopping.set()
        try:
            if self._pipe is not None:
                self.command("quit", timeout=1.0, raise_on_error=False)
        except Exception:  # pragma: no cover - best effort shutdown
            pass
        pipe, self._pipe = self._pipe, None
        if pipe is not None:
            try:
                pipe.close()
            except OSError:  # pragma: no cover
                pass
        process, self._process = self._process, None
        if process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None

    # -- IPC ------------------------------------------------------------- #

    def _read_loop(self) -> None:
        """Drain the pipe: route responses to waiters, events to the state."""
        pipe = self._pipe
        while pipe is not None and not self._stopping.is_set():
            try:
                line = pipe.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            request_id = payload.get("request_id")
            if request_id is not None:
                waiter = self._pending.get(int(request_id))
                if waiter is not None:
                    try:
                        waiter.put_nowait(payload)
                    except queue.Full:  # pragma: no cover
                        pass
                continue
            event = payload.get("event")
            if event:
                self._handle_event(str(event))
        LOGGER.debug("mpv IPC reader finished")
        self._playing = False
        for waiter in list(self._pending.values()):
            try:
                waiter.put_nowait({"error": "mpv IPC pipe closed"})
            except queue.Full:  # pragma: no cover
                pass

    def _handle_event(self, event: str) -> None:
        if event in ("playback-restart", "unpause", "start-file"):
            self._playing = True
        elif event in ("end-file", "idle", "shutdown"):
            self._playing = False

    def command(self, *args: Any, timeout: float = 5.0, raise_on_error: bool = True) -> Any:
        """Send one IPC command and return its ``data`` payload."""
        with self._lock:
            if self._pipe is None:
                raise PlayerError("mpv is not running")
            request_id = next(self._ids)
            waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
            message = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
            try:
                self._pipe.write(message.encode("utf-8"))
            except OSError as exc:
                self._pending.pop(request_id, None)
                self._pipe = None
                raise PlayerError(f"lost the mpv IPC pipe: {exc}") from exc
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty:
            raise PlayerError(f"mpv did not answer {args!r} within {timeout:.0f}s") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        error = response.get("error")
        if error not in (None, "success"):
            if raise_on_error:
                raise PlayerError(f"mpv refused {args!r}: {error}")
            LOGGER.debug("mpv error for %r: %s", args, error)
        return response.get("data")

    # -- playback -------------------------------------------------------- #

    def _apply_headers(self, headers: dict[str, str]) -> None:
        if not headers:
            return
        joined = ",".join(f"{key}: {value}" for key, value in headers.items())
        self.command("set_property", "http-header-fields", joined, raise_on_error=False)

    def load(self, stream: ResolvedStream | str, mode: str = "replace") -> None:
        """Load one item; *mode* is ``replace`` or ``append``."""
        if isinstance(stream, ResolvedStream):
            self._apply_headers(stream.headers)
            target = stream.url
        else:
            target = str(stream)
        self.command("loadfile", target, mode, timeout=20.0)
        if mode == "replace":
            self.command("set_property", "pause", False, raise_on_error=False)

    def play_results(self, results: Sequence[SearchResult], searcher: Searcher) -> None:
        """Replace the queue with *results* and start item 1 once it resolves."""
        if not results:
            raise PlayerError("nothing to play")
        self._queue_generation += 1
        generation = self._queue_generation
        first = searcher.resolve(results[0])
        self.command("playlist-clear", raise_on_error=False)
        self.load(first, "replace")
        LOGGER.info("playing: %s", results[0].title)
        if len(results) > 1:
            threading.Thread(
                target=self._preload,
                args=(list(results[1:]), searcher, generation),
                name="mpv-preload",
                daemon=True,
            ).start()

    def _preload(self, items: list[SearchResult], searcher: Searcher, generation: int) -> None:
        """Resolve the remaining hits in the background so 'next' is instant."""
        for item in items:
            if generation != self._queue_generation or not self.alive:
                LOGGER.debug("preload cancelled (queue replaced or mpv gone)")
                return
            try:
                stream = searcher.resolve(item)
            except SearchError as exc:
                LOGGER.warning("could not pre-resolve %r: %s", item.title, exc)
                continue
            if generation != self._queue_generation or not self.alive:
                return
            try:
                self.load(stream, "append")
                LOGGER.info("queued: %s", item.title)
            except PlayerError as exc:
                LOGGER.warning("could not queue %r: %s", item.title, exc)
                return

    def next_track(self) -> bool:
        self.command("playlist-next")
        return True

    def stop(self) -> bool:
        self.command("stop", raise_on_error=False)
        self._playing = False
        return True

    def volume(self) -> float:
        value = self.command("get_property", "volume", timeout=3.0)
        return float(value) if value is not None else 0.0

    def set_volume(self, value: float) -> float:
        """Set volume clamped to the configured range (plan2.md §7: 0-130)."""
        target = max(float(self.volume_min), min(float(self.volume_max), float(value)))
        self.command("set_property", "volume", target)
        return target

    def volume_delta(self, delta: float) -> bool:
        self.set_volume(self.volume() + float(delta))
        return True

    def is_playing(self) -> bool:
        """True while mpv is loaded and not paused."""
        if self._pipe is None:
            return False
        try:
            paused = self.command("get_property", "pause", timeout=2.0, raise_on_error=False)
            position = self.command(
                "get_property", "playlist-pos", timeout=2.0, raise_on_error=False
            )
        except PlayerError:
            return False
        if position is None:
            return False
        return not bool(paused)

    @property
    def playing(self) -> bool:
        """Event-tracked playback state: free to read, no IPC round trip.

        Updated by the ``playback-restart`` / ``end-file`` / ``idle`` events the
        reader thread sees, which is what the per-block anti-lyrics guard wants.
        Use :meth:`is_playing` when an authoritative answer is needed.
        """
        return self._playing

    def current_title(self) -> str | None:
        try:
            value = self.command("get_property", "media-title", timeout=2.0, raise_on_error=False)
        except PlayerError:
            return None
        return str(value) if value else None

    def status(self) -> MpvStatus:
        try:
            paused = self.command("get_property", "pause", timeout=2.0, raise_on_error=False)
            position = self.command(
                "get_property", "playlist-pos", timeout=2.0, raise_on_error=False
            )
        except PlayerError:
            return MpvStatus(playing=False, playlist_pos=None, volume=None, title=None)
        return MpvStatus(
            playing=position is not None and not bool(paused),
            playlist_pos=int(position) if position is not None else None,
            volume=round(self.volume(), 1),
            title=self.current_title(),
        )
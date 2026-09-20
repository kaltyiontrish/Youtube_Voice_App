"""Persistent mpv process driven over JSON IPC (plan2.md §7).

One mpv instance is spawned at daemon start with ``--idle=yes`` and an
``--input-ipc-server`` pipe; it stays alive between commands and is restarted
only if the pipe dies.  Command replies are read back synchronously on the
same connection that sent the request, while a second connection carries the
broadcast events into the playing state - the historical single-handle design
deadlocked on Windows, where one blocking reader starves every writer.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
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
        self._pipe: Any = None        # command connection: write + read own reply
        self._event_pipe: Any = None  # event connection: owned by the reader thread
        self._reader: threading.Thread | None = None
        self._cmd_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._playing = False
        self._queue_generation = 0
        self._stopping = threading.Event()
        self._reader_stop = threading.Event()
        self._reader_done = threading.Event()
        # Display titles per playlist index: direct googlevideo URLs have no
        # metadata, so media-title would show the raw URL.  The reader thread
        # re-applies the matching title on every playback-restart event.
        self._title_lock = threading.Lock()
        self._track_titles: list[str] = []

    # -- lifecycle ------------------------------------------------------- #

    def mpv_binary(self) -> str:
        """Resolve the mpv executable, including a bare name on PATH."""
        resolved = shutil.which(self.mpv_path)
        if resolved:
            return resolved
        candidate = Path(self.mpv_path)
        if not candidate.is_absolute() and not candidate.is_file():
            # `mpv` is often installed as a directory path-lessly, so check the
            # local fallback before raising the usual PATH error.
            exe = "mpv.exe" if sys.platform.startswith("win") else "mpv"
            for root in (Path(__file__).resolve().parent.parent, Path.cwd()):
                fallback = root / "tools" / "mpv" / exe
                if fallback.is_file():
                    return str(fallback)
        if candidate.is_file():
            return str(candidate)
        raise PlayerError(
            f"mpv executable {self.mpv_path!r} not found; install mpv and put it on PATH, "
            f"drop the portable build into tools/mpv/, or set player.mpv_path in config.yaml"
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
        # voiceyt resolves direct media URLs itself (Searcher.resolve); mpv's
        # ytdl hook would re-extract the googlevideo URL - slow and dependent
        # on a JS runtime - so switch it off.  extra_args may override (last
        # option wins in mpv).
        args.append("--ytdl=no")
        args.extend(self.extra_args)

        LOGGER.debug("starting mpv: %s", " ".join(args))
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        self._process = subprocess.Popen(  # noqa: S603 - the path comes from config
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        if sys.platform.startswith("win"):
            from .winjob import assign_kill_on_close

            # kernel-level safety net: whatever kills voiceyt (console X,
            # crash, taskkill) also kills mpv, so music can never outlive us
            if not assign_kill_on_close(self._process._handle):
                LOGGER.warning("mpv is not in a kill-on-close job; if voiceyt "
                               "dies unexpectedly mpv may keep playing")
        self._stopping.clear()
        self._reader_stop.clear()
        # Two independent connections: a synchronous pipe handle blocks on both
        # reads and writes, so the event reader and command writer cannot share
        # one handle without wedging each other.  mpv supports multiple IPC
        # clients; it broadcasts events to each connection and sends each reply
        # only to the asking client.
        self._pipe = self._open_verified_connection()
        self._event_pipe = self._open_verified_connection()
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._event_pipe,), name="mpv-ipc", daemon=True
        )
        self._reader.start()
        return self

    def _open_verified_connection(self) -> Any:
        """Open an IPC connection that mpv actually answers on.

        mpv creates the named pipe before its IPC loop services it; a client
        that connects in that window blocks forever on its first write.  So
        connect in a worker thread, ping mpv, and only keep a connection that
        replies inside the probe window.  Verified pings also confirm both
        connections survive mpv's early startup.
        """
        deadline = time.monotonic() + self.start_timeout_s
        last_error: Exception | None = None
        ping = b'{"command":["get_property","mpv-version"],"request_id":0}\n'
        attempt = 0
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise PlayerError(
                    f"mpv exited immediately with code {self._process.returncode}; "
                    f"check player.mpv_path and player.extra_args"
                )
            result: dict[str, Any] = {}

            def probe() -> None:
                pipe = None
                try:
                    pipe = open(self.ipc_path, "r+b", buffering=0)
                    pipe.write(ping)
                    line = pipe.readline()
                    if not line:
                        result["error"] = OSError("pipe accepted the ping but never answered")
                        return
                    result["pipe"] = pipe
                except OSError as exc:
                    result["error"] = exc
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass

            probe_thread = threading.Thread(target=probe, name="mpv-ipc-probe", daemon=True)
            probe_thread.start()
            # Bounded: a wedged probe is abandoned (its handle is closed once
            # mpv finishes init and EOFs it, or on GC) and we simply open a
            # fresh connection on the next iteration.
            probe_thread.join(timeout=2.0)
            if "pipe" in result:
                if attempt > 0:
                    LOGGER.debug("IPC connection verified after %d attempt(s)", attempt + 1)
                return result["pipe"]
            last_error = result.get("error") or TimeoutError("probe did not finish")
            attempt += 1
            time.sleep(0.2)
        LOGGER.error("mpv IPC probe failed: %s", last_error)
        raise PlayerError(
            f"mpv did not answer on its IPC pipe {self.ipc_path!r} within "
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
        event_pipe, self._event_pipe = self._event_pipe, None
        if event_pipe is not None and event_pipe is not pipe:
            try:
                event_pipe.close()
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

    def _read_loop(self, event_pipe: Any) -> None:
        """Drain the event connection and flip the playing state.

        This connection never sends commands: every line it receives is a
        broadcast event, so replies to request_id 0 probes are harmless.
        """
        try:
            while not self._stopping.is_set():
                try:
                    line = event_pipe.readline()
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
                # Replies (startup probes) carry no event key; only events do.
                event = payload.get("event")
                if event:
                    self._handle_event(str(event))
        finally:
            LOGGER.debug("mpv IPC reader finished")
            self._playing = False
            if event_pipe is self._event_pipe:
                self._event_pipe = None
            try:
                if event_pipe is not self._pipe:
                    event_pipe.close()
            except (OSError, ValueError):  # pragma: no cover - best effort
                pass

    def _handle_event(self, event: str) -> None:
        if event in ("playback-restart", "unpause", "start-file"):
            self._playing = True
        elif event in ("end-file", "idle", "shutdown"):
            self._playing = False

    def _reconnect_command_locked(self) -> Any:
        """Reopen the command connection; the caller must hold ``_cmd_lock``."""
        process = self._process
        if process is None or self._stopping.is_set():
            raise PlayerError("mpv is not running")
        if process.poll() is not None:
            raise PlayerError(f"mpv exited with code {process.returncode}; restart it")
        pipe = self._open_verified_connection()
        self._pipe = pipe
        return pipe

    def command(self, *args: Any, timeout: float = 5.0, raise_on_error: bool = True) -> Any:
        """Send one IPC command on the command connection, read its reply.

        The command connection is never read by anyone else, so the reply to
        this request is simply the next matching line on it.  Calls are
        serialised under one lock (foreground player calls plus the background
        preload thread) so replies always match the command just sent.

        mpv broadcasts events to every IPC client, so this connection also
        receives event lines; only the line echoing our ``request_id`` is the
        reply, everything else is dropped (the dedicated event connection
        handles the same events anyway).

        The exchange runs in a worker thread and is joined with a deadline: on
        Windows a synchronous pipe read cannot be made non-blocking, so the
        only bounded option is to abandon the worker (a daemon thread wedged
        on a handle mpv will EOF or GC will reap) and detach the handle so the
        next call opens a fresh, verified connection.
        """
        with self._cmd_lock:
            pipe = self._pipe
            if pipe is None or getattr(pipe, "closed", False):
                try:
                    pipe = self._reconnect_command_locked()
                except PlayerError:
                    raise PlayerError("mpv is not running") from None
            request_id = next(self._ids)
            message = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
            result: dict[str, Any] = {}

            def exchange() -> None:
                try:
                    pipe.write(message.encode("utf-8"))
                    buf = b""
                    while True:
                        chunk = pipe.read(4096)
                        if chunk == b"":
                            result["error"] = "mpv closed the connection"
                            return
                        buf += chunk
                        while b"\n" in buf:
                            line, _, buf = buf.partition(b"\n")
                            try:
                                payload = json.loads(line.decode("utf-8", "replace"))
                            except json.JSONDecodeError:
                                continue
                            if (
                                isinstance(payload, dict)
                                and payload.get("request_id") == request_id
                            ):
                                result["response"] = payload
                                return
                            # Event or foreign reply on this connection: drop it.
                except (OSError, ValueError) as exc:
                    result["error"] = str(exc)

            worker = threading.Thread(target=exchange, name="mpv-ipc-cmd", daemon=True)
            worker.start()
            worker.join(timeout=timeout)
            if "response" not in result:
                # Timed out, errored, or mpv wedged: detach the handle so the
                # next call opens a fresh connection instead of reading a
                # stale reply.  The abandoned worker dies with mpv (EOF) or
                # the interpreter (daemon thread).
                if pipe is self._pipe:
                    self._pipe = None
                if "error" in result:
                    raise PlayerError(f"lost the mpv IPC pipe: {result['error']}")
                raise PlayerError(f"mpv did not answer {args!r} within {timeout:.0f}s")
            response = result["response"]
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
        # Direct googlevideo URLs carry no metadata, so mpv would show the raw
        # URL as the track name; titles are re-applied per track on
        # playback-restart events (see _apply_track_title).
        with self._title_lock:
            self._track_titles = [results[0].title]
        self.command("playlist-clear", raise_on_error=False)
        self.load(first, "replace")
        LOGGER.info("playing: %s", results[0].title)
        if not self._wait_for_start(6.0):
            # Rare race: the load is accepted but the stream never starts
            # (transient fetch failure).  Re-resolve and try exactly once.
            LOGGER.warning("track did not start; re-resolving once: %s", results[0].title)
            try:
                self.load(searcher.resolve(results[0]), "replace")
            except SearchError as exc:
                raise PlayerError(f"could not re-resolve {results[0].title!r}: {exc}") from exc
            if not self._wait_for_start(6.0):
                LOGGER.warning("track still not playing; leaving it to mpv")
        if len(results) > 1:
            threading.Thread(
                target=self._preload,
                args=(list(results[1:]), searcher, generation),
                name="mpv-preload",
                daemon=True,
            ).start()

    def _wait_for_start(self, timeout: float) -> bool:
        """True once mpv reports a playlist position and playing state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            try:
                position = self.command(
                    "get_property", "playlist-pos", timeout=2.0, raise_on_error=False
                )
                if isinstance(position, int) and position >= 0:
                    return True
            except PlayerError:
                return False
            time.sleep(0.25)
        return False

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
                with self._title_lock:
                    self._track_titles.append(item.title)
                LOGGER.info("queued: %s", item.title)
            except PlayerError as exc:
                LOGGER.warning("could not queue %r: %s", item.title, exc)
                return

    def next_track(self) -> bool:
        self.command("playlist-next")
        return True

    def prev_track(self) -> bool:
        self.command("playlist-prev")
        return True

    def jump_to(self, index: int) -> bool:
        """Jump to playlist entry *index* (0-based; used by the UI playlist)."""
        self.command("playlist-play-index", int(index))
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
        # After `stop` mpv reports playlist-pos = -1 while idle; only a real
        # entry (>= 0) counts as playing.
        if position is None or int(position) < 0:
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
        """Friendly title of the current entry, tracked locally per playlist index.

        mpv's ``media-title`` for a direct googlevideo URL is the raw URL tail
        and its metadata updates clobber any value we set, so we keep our own
        list (see :meth:`play_results`) and map ``playlist-pos`` into it.
        """
        try:
            position = self.command(
                "get_property", "playlist-pos", timeout=2.0, raise_on_error=False
            )
        except PlayerError:
            return None
        if not isinstance(position, int) or position < 0:
            return None
        with self._title_lock:
            titles = list(self._track_titles)
        if 0 <= position < len(titles):
            return titles[position]
        return None

    def pool_titles(self) -> list[str]:
        """Thread-safe snapshot of the 5-result pool titles (for the UI playlist).

        Returns a shallow copy; callers can read without holding any lock.
        """
        with self._title_lock:
            return list(self._track_titles)

    def status(self) -> MpvStatus:
        try:
            paused = self.command("get_property", "pause", timeout=2.0, raise_on_error=False)
            position = self.command(
                "get_property", "playlist-pos", timeout=2.0, raise_on_error=False
            )
        except PlayerError:
            return MpvStatus(playing=False, playlist_pos=None, volume=None, title=None)
        pos = int(position) if position is not None else None
        return MpvStatus(
            # playlist-pos is -1 when the playlist is empty/stopped: not playing
            playing=pos is not None and pos >= 0 and not bool(paused),
            playlist_pos=pos,
            volume=round(self.volume(), 1),
            title=self.current_title(),
        )
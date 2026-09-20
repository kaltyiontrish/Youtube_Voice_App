"""Microphone capture (plan2.md §5).

16 kHz mono float32 through ``sounddevice``.  The audio callback does the
absolute minimum: one copy into a bounded queue plus a cheap RMS update.  It
never blocks, never logs and never calls into the ASR - when the queue is full
the block is dropped and counted instead.

Consumers work on fixed-size blocks (32 ms by default, which is what Silero VAD
wants), so nothing downstream has to deal with partial frames.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Iterator

import numpy as np
import sounddevice as sd

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    default_samplerate: float
    host_api: str
    is_default: bool


class RingBuffer:
    """Single-producer/single-consumer float32 ring buffer for one channel."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._data = np.zeros(capacity, dtype=np.float32)
        self._lock = threading.Lock()
        self._write_index = 0
        self._filled = 0

    @property
    def capacity(self) -> int:
        return int(self._data.shape[0])

    def write(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return
        if block.size >= self.capacity:
            block = block[-self.capacity :]
        with self._lock:
            end = self._write_index + block.size
            if end <= self.capacity:
                self._data[self._write_index : end] = block
            else:
                split = self.capacity - self._write_index
                self._data[self._write_index :] = block[:split]
                self._data[: end - self.capacity] = block[split:]
            self._write_index = end % self.capacity
            self._filled = min(self.capacity, self._filled + block.size)

    def latest(self, count: int) -> np.ndarray:
        """Most recent *count* samples, oldest first (shorter until filled)."""
        with self._lock:
            available = min(count, self._filled)
            if available <= 0:
                return np.zeros(0, dtype=np.float32)
            start = (self._write_index - available) % self.capacity
            if start + available <= self.capacity:
                return self._data[start : start + available].copy()
            split = self.capacity - start
            return np.concatenate((self._data[start:], self._data[: available - split]))

    def clear(self) -> None:
        with self._lock:
            self._write_index = 0
            self._filled = 0


def _preferred_hostapis() -> set[int]:
    """Host-API indices to list: one entry per physical device, no junk.

    Windows exposes every device through 4 APIs (MME, DirectSound, WASAPI,
    WDM-KS) so the naive list shows each microphone 3-4 times, and WDM-KS
    adds broken entries (PC Speaker, Stereo Mix).  WASAPI is the API Windows
    Settings itself uses; macOS equivalent is Core Audio.
    """
    try:
        named = {str(api["name"]): i for i, api in enumerate(sd.query_hostapis())}
    except Exception:  # pragma: no cover - no host API available
        return set()
    for api_name in ("Windows WASAPI", "Core Audio"):
        index = named.get(api_name)
        if index is not None:
            return {index}
    return set(named.values())  # linux/unknown: everything


def list_input_devices() -> list[DeviceInfo]:
    """Usable input devices, in PortAudio index order (one host API only)."""
    try:
        default_input = sd.default.device[0]
    except Exception:  # pragma: no cover - no host API available
        default_input = -1
    preferred = _preferred_hostapis()
    devices: list[DeviceInfo] = []
    for index, raw in enumerate(sd.query_devices()):
        channels = int(raw.get("max_input_channels", 0))
        if channels < 1:
            continue
        hostapi_index = int(raw.get("hostapi", 0))
        if preferred and hostapi_index not in preferred:
            continue  # same device through MME/DirectSound/WDM-KS: noise
        host_api = ""
        try:
            host_api = str(sd.query_hostapis(hostapi_index).get("name", ""))
        except Exception:  # pragma: no cover
            pass
        devices.append(
            DeviceInfo(
                index=index,
                name=str(raw.get("name", f"device {index}")),
                channels=channels,
                default_samplerate=float(raw.get("default_samplerate", 0.0)),
                host_api=host_api,
                is_default=(index == default_input),
            )
        )
    return devices


def resolve_device(spec: int | str | None) -> tuple[int | None, str]:
    """Turn a config device (``None``, index or name substring) into an index."""
    if spec is None:
        return None, "system default"
    if isinstance(spec, int):
        return spec, f"index {spec}"
    wanted = spec.lower()
    devices = list_input_devices()
    for device in devices:
        if device.name.lower() == wanted:
            return device.index, device.name
    for device in devices:
        if wanted in device.name.lower():
            return device.index, device.name
    raise ValueError(f"no input device matches {spec!r}; run --list-devices")


class AudioCapture:
    """Microphone capture that never blocks the audio callback."""

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = 16000,
        block_ms: int = 32,
        max_queued_blocks: int = 64,
        ring_seconds: float = 2.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.block_frames = max(1, int(self.sample_rate * block_ms / 1000))
        self.device_spec = device
        self.device_index: int | None = None
        self.device_name = "unknown"
        self._queue: Queue[np.ndarray] = Queue(maxsize=max_queued_blocks)
        self._ring = RingBuffer(int(self.sample_rate * ring_seconds))
        self._stream: sd.InputStream | None = None
        self._closed = threading.Event()
        self._level = 0.0
        self._dropped = 0
        self._status = ""

    # -- state ----------------------------------------------------------- #

    @property
    def level(self) -> float:
        """RMS of the most recent block."""
        return self._level

    @property
    def dropped_blocks(self) -> int:
        """Blocks discarded because the consumer fell behind."""
        return self._dropped

    @property
    def status_message(self) -> str:
        return self._status

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- lifecycle ------------------------------------------------------- #

    def open(self) -> "AudioCapture":
        self.device_index, self.device_name = resolve_device(self.device_spec)
        try:
            sd.check_input_settings(
                device=self.device_index,
                channels=1,
                dtype="float32",
                samplerate=self.sample_rate,
            )
        except Exception as exc:
            raise RuntimeError(
                f"cannot capture {self.sample_rate} Hz mono float32 from "
                f"{self.device_name!r}: {exc}"
            ) from exc
        self._stream = sd.InputStream(
            device=self.device_index,
            channels=1,
            dtype="float32",
            samplerate=self.sample_rate,
            blocksize=self.block_frames,
            callback=self._callback,
        )
        self._stream.start()
        return self

    def close(self) -> None:
        self._closed.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def reopen(self, device: int | str | None = None) -> str:
        """Move a live capture to *device* (used by the UI microphone picker).

        Only the PortAudio stream is replaced: ``_closed`` stays clear, so the
        consumer loop in :meth:`blocks` keeps running and the assistant simply
        resumes on the new device.  Blocks still queued from the old device are
        dropped and the caller's own audio pipeline must be reset separately
        (see ``Listener.pause_event``).
        """
        previous = self.device_spec
        if device is not None:
            self.device_spec = device
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        try:
            self.drain()
            return self.open().device_name
        except Exception:
            # Best effort: get the previous device back before reporting.
            self.device_spec = previous
            LOGGER.error("cannot open device %r; back to %r", device, previous)
            self.open()
            raise

    def __enter__(self) -> "AudioCapture":
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- audio callback -------------------------------------------------- #

    def _callback(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        if status:
            self._status = str(status)
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        self._ring.write(block)
        self._level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(block)
        except Full:
            self._dropped += 1

    # -- consumption ----------------------------------------------------- #

    def read_block(self, timeout: float = 0.5) -> np.ndarray | None:
        """Next block, or ``None`` when *timeout* expires."""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def blocks(self, timeout: float = 0.5) -> Iterator[np.ndarray]:
        """Yield blocks until :meth:`close` is called."""
        while not self._closed.is_set():
            block = self.read_block(timeout=timeout)
            if block is not None:
                yield block

    def recent(self, seconds: float) -> np.ndarray:
        """Most recent *seconds* of audio (level meters, diagnostics)."""
        return self._ring.latest(int(self.sample_rate * seconds))

    def record(self, seconds: float) -> np.ndarray:
        """Record exactly *seconds* of audio (used by ``--bench-live``)."""
        needed = int(self.sample_rate * seconds)
        collected: list[np.ndarray] = []
        total = 0
        while total < needed and not self._closed.is_set():
            block = self.read_block(timeout=1.0)
            if block is None:
                break
            collected.append(block)
            total += block.size
        if not collected:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(collected)[:needed]

    def drain(self) -> int:
        """Discard queued blocks; returns how many were dropped."""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except Empty:
                return dropped
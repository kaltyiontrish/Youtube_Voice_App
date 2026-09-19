"""Voice activity detection (plan2.md §5).

Silero VAD driven straight by ``onnxruntime``: the ONNX graph is ~2 MB and needs
no PyTorch.  The ``silero-vad`` PyPI package would pull in torch *and* torchaudio
(~2.5 GB) just to run a 2 MB model, which is exactly what this project avoids.

``SileroVad`` consumes exactly 512-sample windows at 16 kHz and carries the
recurrent state between calls.  ``UtteranceSegmenter`` turns those per-window
probabilities into complete utterances for the batch backends (whisper,
parakeet) and doubles as the power gate for the streaming backend (nemotron).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort

LOGGER = logging.getLogger(__name__)

WINDOW_SAMPLES = 512  # Silero's fixed step size at 16 kHz (32 ms)
PRE_ROLL_MS = 200     # audio kept from before speech was detected


@dataclass(frozen=True)
class Utterance:
    """A speech segment plus the stream timestamps it covers."""

    audio: np.ndarray
    start_ts: float
    end_ts: float
    speech_ms: float

    @property
    def duration_ms(self) -> float:
        """Total length in ms, including the padded silence."""
        return float(self.audio.size) * 1000.0 / 16000.0


class SileroVad:
    """Minimal stateful wrapper around the Silero ONNX graph."""

    def __init__(
        self,
        model_path: str | None = None,
        providers: list[str] | None = None,
    ) -> None:
        if not model_path:
            raise ValueError("model_path is required for SileroVad")
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=providers or ["CPUExecutionProvider"],
        )
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._has_sr = "sr" in self._input_names
        self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> np.ndarray:
        return np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        self._state = self._empty_state()

    def probability(self, window: np.ndarray) -> float:
        """Speech probability for exactly one 512-sample window."""
        samples = np.asarray(window, dtype=np.float32).reshape(-1)
        if samples.size != WINDOW_SAMPLES:
            raise ValueError(f"Silero needs exactly {WINDOW_SAMPLES} samples")
        feeds: dict[str, np.ndarray] = {
            "input": samples.reshape(1, -1),
            "state": self._state,
        }
        if self._has_sr:
            feeds["sr"] = np.array(16000, dtype=np.int64)
        outputs = self._session.run(None, feeds)
        self._state = np.asarray(outputs[1], dtype=np.float32)
        return float(np.asarray(outputs[0]).reshape(-1)[0])

    def probability_of(self, audio: np.ndarray) -> float:
        """Highest window probability across *audio* (used by diagnostics)."""
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.reset()
        best = 0.0
        for start in range(0, max(0, samples.size - WINDOW_SAMPLES + 1), WINDOW_SAMPLES):
            best = max(best, self.probability(samples[start : start + WINDOW_SAMPLES]))
        self.reset()
        return best


class UtteranceSegmenter:
    """Groups VAD-positive windows into utterances bounded by silence."""

    def __init__(
        self,
        vad: SileroVad,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_speech_ms: int = 150,
        min_silence_ms: int = 400,
        max_utterance_s: float = 25.0,
    ) -> None:
        self.vad = vad
        self.sample_rate = int(sample_rate)
        self.threshold = float(threshold)
        self.min_speech_ms = float(min_speech_ms)
        self.min_silence_ms = float(min_silence_ms)
        self.max_utterance_ms = float(max_utterance_s) * 1000.0
        self.window_ms = WINDOW_SAMPLES * 1000.0 / self.sample_rate
        self._pre_roll_windows = max(1, int(round(PRE_ROLL_MS / self.window_ms)))

        self._samples_fed = 0
        self._buffer: list[np.ndarray] = []
        self._triggered = False
        self._speech_windows = 0
        self._silence_windows = 0
        self._segment_start_ts = 0.0
        self._residual = np.zeros(0, dtype=np.float32)
        self._idle_silence_ms = 0.0

    # -- state ----------------------------------------------------------- #

    @property
    def stream_ts(self) -> float:
        """Timestamp of the newest sample fed to the segmenter."""
        return self._samples_fed / float(self.sample_rate)

    @property
    def current_silence_ms(self) -> float:
        """Trailing silence in ms, or 0 while speech is being accumulated."""
        if self._triggered:
            return self._silence_windows * self.window_ms
        return 0.0

    @property
    def speaking(self) -> bool:
        """True between speech onset and utterance emission."""
        return self._triggered

    @property
    def trailing_silence_ms(self) -> float:
        """Silence since the last speech window, whether or not we are inside
        an utterance.  This is what drives the matcher's ``silence_end_ms``
        timer, so it must keep counting after an utterance was emitted."""
        if self._triggered:
            return self._silence_windows * self.window_ms
        return self._idle_silence_ms

    def reset(self) -> None:
        self._buffer = []
        self._triggered = False
        self._speech_windows = 0
        self._silence_windows = 0
        self._residual = np.zeros(0, dtype=np.float32)
        self._idle_silence_ms = 0.0
        self.vad.reset()

    # -- streaming API --------------------------------------------------- #

    def push(self, chunk: np.ndarray) -> list[Utterance]:
        """Feed arbitrary-length audio; returns any utterances that completed."""
        samples = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return []
        if self._residual.size:
            samples = np.concatenate((self._residual, samples))
        windows = samples.size // WINDOW_SAMPLES
        self._residual = samples[windows * WINDOW_SAMPLES :].copy()

        utterances: list[Utterance] = []
        for index in range(windows):
            window = samples[index * WINDOW_SAMPLES : (index + 1) * WINDOW_SAMPLES]
            probability = self.vad.probability(window)
            self._samples_fed += WINDOW_SAMPLES
            utterance = self._consume_window(window, probability)
            if utterance is not None:
                utterances.append(utterance)
        return utterances

    def _consume_window(self, window: np.ndarray, probability: float) -> Utterance | None:
        is_speech = probability >= self.threshold

        if not self._triggered:
            self._buffer.append(window)
            if len(self._buffer) > self._pre_roll_windows:
                self._buffer.pop(0)
            self._idle_silence_ms = 0.0 if is_speech else self._idle_silence_ms + self.window_ms
            if is_speech:
                self._triggered = True
                self._speech_windows = 1
                self._silence_windows = 0
                self._segment_start_ts = max(
                    0.0, self.stream_ts - len(self._buffer) * self.window_ms / 1000.0
                )
            return None

        self._buffer.append(window)
        if is_speech:
            self._speech_windows += 1
            self._silence_windows = 0
        else:
            self._silence_windows += 1

        speech_ms = self._speech_windows * self.window_ms
        buffered_ms = len(self._buffer) * self.window_ms
        if self._silence_windows * self.window_ms >= self.min_silence_ms:
            return self._emit("silence", speech_ms)
        if buffered_ms >= self.max_utterance_ms:
            return self._emit("max utterance length", speech_ms)
        return None

    def _emit(self, reason: str, speech_ms: float) -> Utterance | None:
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0, dtype=np.float32)
        end_ts = self.stream_ts - self._silence_windows * self.window_ms / 1000.0
        start_ts = self._segment_start_ts
        self._buffer = []
        self._triggered = False
        self._speech_windows = 0
        self._idle_silence_ms = self._silence_windows * self.window_ms
        self._silence_windows = 0
        if speech_ms < self.min_speech_ms:
            LOGGER.debug("dropping %.0f ms of speech (ended by %s)", speech_ms, reason)
            return None
        LOGGER.debug(
            "utterance %.2fs-%.2fs (%.0f ms speech, ended by %s)",
            start_ts,
            end_ts,
            speech_ms,
            reason,
        )
        return Utterance(audio=audio, start_ts=start_ts, end_ts=end_ts, speech_ms=speech_ms)

    def flush(self) -> Utterance | None:
        """Force the in-progress utterance out (shutdown, diagnostics)."""
        if not self._triggered or not self._buffer:
            return None
        return self._emit("flush", self._speech_windows * self.window_ms)
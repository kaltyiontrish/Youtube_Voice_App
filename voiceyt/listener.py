"""Audio capture, VAD segmentation, and ASR dispatch for voiceyt."""

from __future__ import annotations

import logging

import numpy as np

from .config import Config

LOGGER = logging.getLogger("voiceyt")


class Listener:
    """audio -> AEC -> VAD -> ASR -> callbacks (plan2.md Â§5).

    Batch backends (whisper, parakeet) are handed one complete utterance from the
    VAD segmenter.  The streaming backend (nemotron) receives the live blocks
    while the VAD reports speech, is flushed when the utterance ends, and reports
    its growing hypothesis through ``on_partial`` for the journal.

    ``on_silence(ts, silence_ms)`` fires for every block so the caller can run the
    matcher's timers - that is what ends a query early (``silence_end_ms``).

    ``pause_event`` (optional) is checked on every block: while it is set the
    audio is dropped instead of transcribed, so the UI can mute the assistant
    without stopping the capture or losing the stream clock.

    Audio/VAD imports are deliberately local: ``--list-devices`` must not pay for
    importing onnxruntime.
    """

    def __init__(
        self,
        config: Config,
        backend,
        *,
        on_utterance,
        on_partial=None,
        on_silence=None,
        canceller=None,
        pause_event=None,
    ) -> None:
        from .audio import AudioCapture

        self.config = config
        self.backend = backend
        self.on_utterance = on_utterance
        self.on_partial = on_partial
        self.on_silence = on_silence
        self.canceller = canceller
        self.pause_event = pause_event
        self.capture = AudioCapture(
            device=config.audio.device, sample_rate=config.audio.sample_rate
        )
        self.segmenter = None
        self._samples_seen = 0
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._stopped = False
        self._paused_state = False

    @property
    def stream_ts(self) -> float:
        """Timestamp of the newest sample processed, in seconds."""
        return self._samples_seen / float(self.config.audio.sample_rate)

    @property
    def window_samples(self) -> int:
        """Fallback window length used when the VAD is disabled."""
        return int(self.config.vad.max_utterance_s * self.config.audio.sample_rate)

    # -- lifecycle ------------------------------------------------------- #

    def open(self) -> "Listener":
        if self.config.vad.enabled:
            from .vad import SileroVad, UtteranceSegmenter

            vad = SileroVad(str(self.config.vad.model_path))
            self.segmenter = UtteranceSegmenter(
                vad,
                sample_rate=self.config.audio.sample_rate,
                threshold=self.config.vad.threshold,
                min_speech_ms=self.config.vad.min_speech_ms,
                min_silence_ms=self.config.vad.min_silence_ms,
                max_utterance_s=self.config.vad.max_utterance_s,
            )
        else:
            LOGGER.warning(
                "vad.enabled is false: falling back to %.0fs fixed windows with no "
                "silence detection",
                self.config.vad.max_utterance_s,
            )
        self.capture.open()
        LOGGER.info(
            "listening on %s (%d Hz mono, backend=%s)",
            self.capture.device_name,
            self.config.audio.sample_rate,
            self.backend.name,
        )
        return self

    def stop(self) -> None:
        self._stopped = True
        self.capture.close()

    def run(self) -> None:
        """Blocking capture loop until :meth:`stop` or ``Ctrl-C``."""
        try:
            for block in self.capture.blocks():
                if self._stopped:
                    break
                self.process(block)
        finally:
            self.flush()

    # -- processing ------------------------------------------------------ #

    def process(self, block: np.ndarray) -> None:
        """Handle one captured block: AEC, VAD, then ASR."""
        if self.pause_event is not None and self.pause_event.is_set():
            self._drop_while_paused(block)
            return
        if self._paused_state:  # just resumed: start from a clean slate
            self._paused_state = False
            self._reset_pipeline()
        samples = self.canceller.process(block) if self.canceller is not None else block
        self._samples_seen += samples.size
        if self.segmenter is None:
            self._process_without_vad(samples)
            return

        utterances = self.segmenter.push(samples)
        if self.on_silence is not None:
            self.on_silence(self.stream_ts, self.segmenter.trailing_silence_ms)

        if self.backend.streaming:
            if self.segmenter.speaking:
                self._emit_partial(samples)
            for utterance in utterances:
                self._finish_stream(utterance.end_ts)
        else:
            for utterance in utterances:
                self._finish_batch(utterance.audio, utterance.end_ts)

    def _emit_partial(self, samples: np.ndarray) -> None:
        if self.on_partial is None:
            return
        text = self.backend.feed(samples)
        if text:
            self.on_partial(text, self.stream_ts)

    def _finish_stream(self, ts: float) -> None:
        text = (self.backend.flush() or "").strip()
        self.backend.reset()
        if text:
            self.on_utterance(text, ts)

    def _finish_batch(self, audio: np.ndarray, ts: float) -> None:
        text = self.backend.transcribe(audio).strip()
        self.backend.reset()
        if text:
            self.on_utterance(text, ts)

    def _process_without_vad(self, samples: np.ndarray) -> None:
        """VAD disabled: stream continuously, or batch on fixed-length windows."""
        if self.backend.streaming:
            self._emit_partial(samples)
            self._pending_samples += samples.size
            if self._pending_samples >= self.window_samples:
                self._finish_stream(self.stream_ts)
                self._pending_samples = 0
            return

        self._pending.append(samples)
        self._pending_samples += samples.size
        if self._pending_samples < self.window_samples:
            return
        audio = np.concatenate(self._pending)
        self._pending.clear()
        self._pending_samples = 0
        self._finish_batch(audio, self.stream_ts)

    def flush(self) -> None:
        """Emit whatever is still buffered (shutdown)."""
        if self.pause_event is not None and self.pause_event.is_set():
            return  # nothing was transcribed while paused
        if self.segmenter is not None:
            if self.backend.streaming:
                self._finish_stream(self.stream_ts)
                return
            utterance = self.segmenter.flush()
            if utterance is not None:
                self._finish_batch(utterance.audio, utterance.end_ts)
            return
        if self._pending:
            audio = np.concatenate(self._pending)
            self._pending.clear()
            self._pending_samples = 0
            self._finish_batch(audio, self.stream_ts)

    # -- pause (driven by the tray/overlay) ------------------------------- #

    def _drop_while_paused(self, block: np.ndarray) -> None:
        """Throw *block* away, keeping the stream clock in real time.

        The first block of a pause also resets the pipeline so an utterance
        can never span the pause (VAD state, AEC state, streamed hypothesis).
        """
        if not self._paused_state:
            self._paused_state = True
            self._reset_pipeline()
        self._samples_seen += block.size

    def _reset_pipeline(self) -> None:
        self._pending.clear()
        self._pending_samples = 0
        if self.segmenter is not None:
            self.segmenter.reset()
        if self.canceller is not None:
            self.canceller.reset()
        # Same as _finish_batch: every backend is told to drop its buffers
        # (a no-op for the batch ones, required for the streaming one).
        self.backend.reset()


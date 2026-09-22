"""Resampling in ``voiceyt.audio.AudioCapture`` (plan2.md §5).

Windows WASAPI opens a capture stream only at the device's native rate
(48 kHz on this machine) and rejects 16 kHz with PaErrorCode -9997, so
:meth:`AudioCapture.open` falls back to the native rate and the callback has
to resample back down.  These tests pin that contract without a microphone:
:meth:`AudioCapture._callback` is driven directly with frames shaped exactly
like the ones PortAudio hands it.
"""

from __future__ import annotations

import unittest

import numpy as np

from voiceyt.audio import AudioCapture

PIPELINE_RATE = 16000
NATIVE_RATE = 48000
BLOCK_MS = 32
NATIVE_BLOCK = int(NATIVE_RATE * BLOCK_MS / 1000)  # 1536 frames


def native_capture() -> AudioCapture:
    """A capture whose stream is open at NATIVE_RATE, like ``open``'s fallback."""
    capture = AudioCapture(sample_rate=PIPELINE_RATE, block_ms=BLOCK_MS)
    capture._device_rate = NATIVE_RATE
    capture._build_resampler(NATIVE_RATE)
    return capture


def tone(count: int, hz: float = 1000.0, rate: int = NATIVE_RATE) -> np.ndarray:
    """*count* samples of a full-scale sine at *hz*, sampled at *rate*."""
    t = np.arange(count, dtype=np.float32) / rate
    return np.sin(2 * np.pi * hz * t).astype(np.float32)


class NativeRateTests(unittest.TestCase):
    def test_callback_queues_pipeline_sized_blocks(self) -> None:
        """1536 native frames in, one 512-frame block out - not the raw 1536."""
        capture = native_capture()
        mono = tone(NATIVE_BLOCK)
        capture._callback(mono[:, None], mono.size, None, None)
        block = capture.read_block(timeout=0)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.size, capture.block_frames)
        self.assertEqual(capture.dropped_blocks, 0)

    def test_blocks_are_continuous_across_callbacks(self) -> None:
        """No frame is lost, duplicated or dropped at a block boundary."""
        capture = native_capture()
        # 700 Hz does not fit a whole number of cycles per block, so a missing
        # or duplicated block would show up as a step in the waveform.
        mono = tone(NATIVE_BLOCK * 12, hz=700.0)
        fed = 0
        for start in range(0, mono.size, NATIVE_BLOCK):
            chunk = mono[start : start + NATIVE_BLOCK]
            capture._callback(chunk[:, None], chunk.size, None, None)
            fed += 1
        blocks = [capture.read_block(timeout=0) for _ in range(fed)]
        self.assertTrue(all(block is not None for block in blocks))
        joined = np.concatenate([b for b in blocks if b is not None])
        # Every callback yielded exactly one full pipeline block.
        self.assertEqual(joined.size, capture.block_frames * fed)
        # A 700 Hz tone at 16 kHz steps by at most 0.28 per sample; a dropped
        # block would jump by about a full peak-to-peak.
        self.assertLess(float(np.max(np.abs(np.diff(joined)))), 0.6)


class PitchTests(unittest.TestCase):
    def test_resampling_keeps_the_original_pitch(self) -> None:
        """A 1 kHz tone stays 1 kHz, i.e. 2000 zero crossings per second."""
        capture = native_capture()
        mono = tone(NATIVE_RATE)  # 1 s at the native rate
        blocks = [
            chunk
            for start in range(0, mono.size, NATIVE_BLOCK)
            for chunk in capture._downsample(mono[start : start + NATIVE_BLOCK])
        ]
        audio = np.concatenate(blocks)
        self.assertGreater(audio.size, PIPELINE_RATE * 0.9)
        crossings = int(np.count_nonzero(np.diff(np.signbit(audio))))
        per_second = crossings / (audio.size / capture.sample_rate)
        self.assertAlmostEqual(per_second, 2000, delta=100)  # 1 kHz -> 2/cycle
        self.assertAlmostEqual(
            float(np.sqrt(np.mean(np.square(audio)))), 0.707, delta=0.05
        )  # a full-scale sine keeps its amplitude

    def test_integer_ratio_leaves_no_residue(self) -> None:
        """48 kHz -> 16 kHz is exactly 3:1, so leftovers must stay bounded."""
        capture = native_capture()
        mono = tone(NATIVE_BLOCK * 30)
        for start in range(0, mono.size, NATIVE_BLOCK):
            capture._downsample(mono[start : start + NATIVE_BLOCK])
        self.assertLess(capture._pending.size, capture.block_frames)


class PipelineRateTests(unittest.TestCase):
    def test_matching_rates_bypass_the_resampler(self) -> None:
        """The 16 kHz path must not pay for filtering it does not need."""
        capture = AudioCapture(sample_rate=PIPELINE_RATE, block_ms=BLOCK_MS)
        self.assertEqual(capture._device_rate, PIPELINE_RATE)
        self.assertIsNone(capture._resamp_taps)
        mono = tone(PIPELINE_RATE * BLOCK_MS // 1000, hz=440.0, rate=PIPELINE_RATE)
        capture._callback(mono[:, None], mono.size, None, None)
        block = capture.read_block(timeout=0)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.size, mono.size)
        self.assertTrue(np.array_equal(block, mono))


if __name__ == "__main__":
    unittest.main()


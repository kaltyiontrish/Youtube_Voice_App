"""Microphone + VAD test for voiceyt (no player, no GitHub, just your mic).

What it does:
  1. opens the microphone (16 kHz mono, same path the daemon uses),
  2. prints a live loudness meter so you can check the right device is on,
  3. runs Silero VAD + the utterance segmenter exactly like the daemon,
  4. prints one line per detected utterance (duration + speech ms),
  5. with --asr, also transcribes each utterance and shows the matched verbs.

Usage (from the project folder):
    .\.venv\Scripts\python.exe mic_test.py                 # 3-second VAD check? no: runs until Ctrl+C
    .\.venv\Scripts\python.exe mic_test.py --seconds 10    # auto-stop after 10 s
    .\.venv\Scripts\python.exe mic_test.py --device 1      # pick a mic (see --list-devices)
    .\.venv\Scripts\python.exe mic_test.py --asr           # + transcription (needs models)
    .\.venv\Scripts\python.exe -m voiceyt --list-devices   # to find your mic index
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from voiceyt.audio import AudioCapture
from voiceyt.config import load_config
from voiceyt.text import normalize  # shows the command words exactly as matched
from voiceyt.vad import SileroVad, UtteranceSegmenter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", type=int, default=None, help="mic index (--list-devices)")
    parser.add_argument("--seconds", type=float, default=None, help="auto-stop after N seconds")
    parser.add_argument("--asr", action="store_true", help="also transcribe (downloads/loads a model)")
    args = parser.parse_args()

    config = load_config()
    model_path = Path(config.vad.model_path)
    if not model_path.is_file():
        print(f"VAD model missing: {model_path}")
        print("run:  .\\.venv\\Scripts\\python.exe -m voiceyt --download-models vad")
        return 1

    backend = None
    if args.asr:
        from voiceyt.asr.registry import load_backend

        print(f"loading ASR backend {config.asr.backend!r} (first run downloads the model) ...")
        try:
            backend = load_backend(config)
        except Exception as exc:
            print(f"ASR load failed ({exc}); continuing VAD-only.")
            backend = None

    vad = SileroVad(str(model_path))
    segmenter = UtteranceSegmenter(
        vad,
        sample_rate=config.audio.sample_rate,
        threshold=config.vad.threshold,
        min_speech_ms=config.vad.min_speech_ms,
        min_silence_ms=config.vad.min_silence_ms,
        max_utterance_s=config.vad.max_utterance_s,
    )

    capture = AudioCapture(device=args.device, sample_rate=config.audio.sample_rate)
    capture.open()
    print(f"mic: {capture.device_name!r} @ {capture.sample_rate} Hz")
    print("speak! (Ctrl+C to stop)\n")

    utterances = 0
    started = time.monotonic()
    last_meter = 0.0
    try:
        while True:
            block = capture.read_block(timeout=0.5)
            if block is None:
                if capture.closed:
                    break
                continue
            for utterance in segmenter.push(block):
                utterances += 1
                line = (
                    f"  >> utterance #{utterances}: {utterance.duration_ms / 1000:.2f}s "
                    f"({utterance.speech_ms:.0f} ms speech)"
                )
                if backend is not None:
                    text = backend.transcribe(utterance.audio).strip()
                    line += f"\n     heard: {text!r} -> normalized: {normalize(text)!r}"
                print(line, flush=True)

            now = time.monotonic()
            if now - last_meter >= 0.5:  # 2 Hz meter
                last_meter = now
                bar = "#" * min(40, int(capture.level * 200))
                state = "SPEAKING" if segmenter.speaking else "quiet"
                print(f"\r[{bar:<40}] rms={capture.level:.3f} {state:<8}", end="", flush=True)
            if args.seconds is not None and now - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        capture.close()
        if backend is not None:
            backend.close()
    print(f"\ndone: {utterances} utterance(s) in {time.monotonic() - started:.1f}s, "
          f"{capture.dropped_blocks} dropped blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

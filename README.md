# voiceyt - voice-controlled YouTube playback (pt-PT)

A background daemon that listens to the microphone all day, ignores normal
speech, and acts only when it hears `youtube` followed by a known verb.
Implementation of [plan2.md](plan2.md).

| Spoken phrase | Action |
|---|---|
| `youtube passa <query>` | search YouTube, play first result |
| `youtube toca <query>` | same (alias verb) |
| `youtube próximo` | play next result from the current search |
| `youtube pára` | stop playback |
| `youtube mais alto` | volume up |
| `youtube mais baixo` | volume down |

The trigger word `youtube` **alone does nothing**: a recognised verb must follow
it within `trigger.trigger_window_ms`. Adding a new phrase is a `config.yaml`
edit, never a code change.

---

## 1. Requirements

| Component | Notes |
|---|---|
| Python | **3.11** (3.10-3.13 all work with these wheels; the venv here is 3.11) |
| mpv | external binary, must be on `PATH` (or set `player.mpv_path`) |
| GPU | optional but assumed: NVIDIA + CUDA 12 driver. This machine is an RTX 2060 (6 GB, driver 546.33 = CUDA 12.3) |
| ffmpeg | optional; only needed for `echoff` hardware checks and some yt-dlp paths |

## 2. Install

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
# onnxruntime-gpu must stay < 1.27 on a CUDA 12 driver: 1.27+ wants CUDA 13
.\.venv\Scripts\python.exe -m pip install "onnxruntime-gpu[cuda,cudnn]<1.27"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The `[cuda,cudnn]` extra installs cuBLAS 12 and cuDNN 9 as pip wheels, so **no
system CUDA toolkit is required**. `voiceyt.dlls` adds those DLL directories to
the loader path automatically before any backend is imported - both CTranslate2
(whisper) and ONNX Runtime need them, and without this step you get
`cublas64_12.dll not found` / `cudnn_ops64_9.dll not found`.

Install mpv system-wide (`winget install mpv` or `scoop install mpv`), or just
drop the portable build into `tools/mpv/` (e.g. from the
[shinchiro win-build releases](https://github.com/shinchiro/mpv-winbuild-cmake/releases) -
the player picks it up automatically). Then check:

```powershell
mpv --version
python -m voiceyt --list-devices
```

### Models

Models are **never** downloaded while the daemon runs. Fetch them explicitly:

```powershell
python -m voiceyt --download-models all        # every backend + the VAD model
python -m voiceyt --download-models parakeet vad
python -m voiceyt --download-models            # same as 'all'
```

Approximate sizes: `whisper` small ~500 MB, `parakeet` ~1 GB, `nemotron` ~2.5 GB,
`vad` ~2.3 MB. If the selected backend's files are missing, the daemon prints the
exact download command and exits.

## 3. Running

```powershell
python -m voiceyt                       # daemon with config default backend
python -m voiceyt --backend nemotron    # override the backend for one run
python -m voiceyt --meter               # M1: live RMS level, Ctrl-C to stop
python -m voiceyt --listen              # M2: transcribe + journal, act on nothing
python -m voiceyt --bench-live 60       # M2: record 60 s, compare all backends
python -m voiceyt --bench logs\bench-live-*.wav
python -m voiceyt --replay-log logs\transcripts.log   # M3: offline matcher run
python -m voiceyt --aec-probe           # M5: echo-cancellation status
```

`--config PATH`, `--log-level LEVEL` and `--backend NAME` apply to every mode.
Transcripts are journalled to `behaviour.log_path` (default `logs/transcripts.log`)
in a tab-separated, greppable format:

```
2026-09-19T18:04:12.345	parakeet	final	youtube passa nirvana
2026-09-19T18:04:14.101	nemotron	partial	youtube passa nir
2026-09-19T18:04:15.002	-	command	action=play query=nirvana reason=query completed in one utterance
```

## 4. Milestone acceptance tests

| Milestone | What to run | What proves it passed |
|---|---|---|
| M1 skeleton | `python -m voiceyt --list-devices`, then `--meter` | devices listed with indices; the level bar reacts to speech and drops in silence |
| M2 backends | `--download-models all`, then `--bench-live 60` while reading [`examples/bench-pt.txt`](examples/bench-pt.txt) aloud | three transcripts side by side with load time, wall time and RTF; you judge which hears `youtube`/`próximo`/`pára` |
| M2 journal | `--listen` for a few hours of ordinary talking | `logs/transcripts.log` becomes the M3 test set |
| M3 matcher | `--replay-log logs/transcripts.log` | **zero** commands fire on real conversation; deliberate phrases all fire |
| M4 player | `python -m voiceyt` (headphones on), speak all six commands | play/next/stop/volume all work end to end |
| M5 echo | music through speakers for 10 minutes | zero false triggers, and `youtube pára` still works over the music |

## 5. Tuning workflow

1. `--listen` with `log_transcripts: true` for a few hours of normal speech.
2. `--replay-log` that journal. Every firing that should not have happened is a
   false positive; add the offending word to `trigger.words`/verbs, or tighten
   `trigger.trigger_window_ms` (smaller = stricter).
3. Re-run `--replay-log` until it is silent, then test the six real commands into
   the mic.
4. Pick the ASR backend from the `--bench-live` table and set `asr.backend`.

## 6. Project layout

```
voiceyt/
  __main__.py     CLI entry point, listening pipeline, run loop
  config.py       load + validate config.yaml, apply defaults
  dlls.py         make the pip CUDA/cuDNN wheels loadable (no system toolkit)
  audio.py        mic capture (sounddevice), 16 kHz mono float32, ring buffer
  vad.py          Silero VAD (onnxruntime) + utterance segmenter
  aec.py          optional echo cancellation (plan2.md §8 step 2)
  text.py         normalization + phrase matching
  matcher.py      trigger/verb state machine
  commands.py     action handlers (play/next/stop/volume_up/volume_down)
  player.py       mpv subprocess + JSON IPC
  search.py       yt-dlp wrapper (search + lazy URL resolution)
  models.py       download, cache, verify models
  bench.py        compare all backends (plan2.md §9, M2)
  transcripts.py  transcript journal + offline replay (M2 -> M3)
  asr/
    base.py       ASRBackend interface
    registry.py   name -> class
    whisper.py    faster-whisper (CTranslate2)
    parakeet.py   onnx-asr, nvidia/parakeet-tdt-0.6b-v3
    nemotron.py   streaming, vendored ONNX engine
  vendor/
    nemotron_onnx_streaming.py   Apache-2.0, see NOTICE
```

Offline unit tests (no microphone, GPU or models needed):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 7. Deviations from plan2.md, and why

1. **Python 3.11 venv instead of the system 3.10.7.** All wheels used here also
   support 3.10, but the plan asks for 3.11+ and the system interpreter is old.
2. **The VAD is the Silero ONNX graph driven by onnxruntime, not the
   `silero-vad` PyPI package.** That package depends on `torch` *and* `torchaudio`
   (~2.5 GB) to run a 2 MB model, which contradicts the plan's "one process, no
   heavy dependency tree" and would dwarf the ASR backends themselves.
3. **Nemotron is driven through a vendored engine.** `onnx-asr` supports
   Parakeet/Canary/GigaAM/Vosk/Whisper but has **no** cache-aware streaming support
   for Nemotron, so `asr/nemotron.py` wraps the community export's NumPy +
   onnxruntime engine, vendored byte-for-byte under `voiceyt/vendor/` (Apache-2.0
   code; weights are NVIDIA OpenMDW-1.1 - see NOTICE). Partial hypotheses are
   journalled but never drive the matcher, because a growing hypothesis would fire
   the same command repeatedly.
4. **Four extra CLI modes**, because the milestone acceptance tests need them:
   `--meter` (M1), `--listen` (M2 journal), `--replay-log` (M3), `--aec-probe` (M5).
5. **`aec.backend: echoff` is not wired as an in-line filter.** echoff owns its own
   WASAPI-loopback capture and returns *matched* frame triples, so it is an audio
   source rather than a filter, and its live path is Windows-only. `voiceclean`
   and `speexdsp` are implemented behind `aec.enabled: true`; both need a playback
   reference from `aec.loopback_device`. Try §8 step 1 first - it is zero code.
6. **Extra config keys**, all documented in `config.yaml`: `behaviour.log_level`,
   `player.volume_min/max/start_timeout_s`, `search.socket_timeout_s`,
   `vad.model_path/min_speech_ms/max_utterance_s`,
   `behaviour.require_same_utterance_while_playing`,
   `asr.whisper.beam_size/condition_on_previous_text`, `aec.frame_ms/stream_delay_ms`.
   Every timing and path still comes from config; no magic numbers in code.
7. **`asr.backend` defaults to `parakeet`** rather than `nemotron`: Parakeet v3 is
   the smaller download and is not under the NVIDIA OpenMDW licence. Switch to
   whichever backend wins the `--bench-live` comparison.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `cublas64_12.dll not found`, `cudnn_ops64_9.dll not found` | CUDA wheels missing: `pip install "onnxruntime-gpu[cuda,cudnn]<1.27"`. `voiceyt.dlls` handles the loader path. |
| `CUDAExecutionProvider` absent from `onnxruntime.get_available_providers()` | The CPU `onnxruntime` is installed instead of `onnxruntime-gpu`, or CUDA 13 wheels are installed on a CUDA 12 driver. |
| `mpv executable 'mpv' not found` | Install mpv and put it on `PATH`, or set `player.mpv_path`. |
| mpv runs but nothing plays | Bot check or region block: set `search.cookies_from_browser` (e.g. `firefox`) and update yt-dlp. |
| `model files are missing ... run: python -m voiceyt --download-models ...` | Run exactly the command it prints; downloads never happen implicitly. |
| A real command does not fire | Check `logs/transcripts.log`: if the word came back differently, add that spelling to `trigger.words` or the verb list in `config.yaml`. |
| Commands fire on music lyrics | Enable `behaviour.require_same_utterance_while_playing` and `ignore_trigger_ms_after_play`, then do plan2.md §8 steps 1 and 2. |
| `youtube passa` fires but nothing is searched | The query was empty (only the verb was heard). An empty query is refused by design; say the query in the same utterance. |
| Mic level always zero | Windows microphone privacy setting, wrong `audio.device` (use `--list-devices`), or a muted device. |

## 9. Licences

See [NOTICE](NOTICE). Most notably the Nemotron weights are **OpenMDW-1.1**, not
Apache/MIT: check that licence before making `nemotron` the default backend.

# plan.md — Voice-controlled YouTube (pt-PT)

A background daemon that listens to the microphone all day, ignores normal speech, and
acts only when it hears `youtube` followed by a known verb.

Target machine: **NVIDIA RTX 4000-series GPU available. Use CUDA by default.**

---

## 0. Rules for whoever implements this

- Python 3.11+. One process. No web server, no LLM, no MCP.
- Call yt-dlp directly as a Python library.
- **Do not hardcode command words in Python.** They live in `config.yaml` (§6).
- Every timing value, path, and model ID comes from config. No magic numbers in code.
- Log every transcript to a file when `log_transcripts: true`. Tuning is impossible without it.
- Build the milestones in order (§9). Each has an acceptance test. Do not skip ahead.

---

## 1. What it does

| Spoken phrase | Action |
|---|---|
| `youtube play <query>` | search YouTube, play first result |
| `youtube toca <query>` | same (alias verb) |
| `youtube próximo` | play next result from the current search |
| `youtube next` | play next result from the current search |
| `youtube pára` | stop playback |
| `youtube stop` | stop playback |
| `youtube mais alto` | volume up |
| `youtube volume up` | volume up |
| `youtube mais baixo` | volume down |
| `youtube volume down` | volume down |

The trigger word `youtube` **alone does nothing**. A recognised verb must follow it within
a time window. This is the main defence against false triggers, because "youtube" appears
in ordinary conversation but "youtube passa" does not.

Adding a new phrase must be a `config.yaml` edit, never a code change.

---

## 2. Architecture

```
microphone ──► AEC (optional) ──► ring buffer ──► VAD ──► ASR backend ──► text
                   ▲                                                       │
        speaker loopback reference                                         ▼
                                                             normalize ──► matcher ──► command
                                                                                        │
                                                                    ┌───────────────────┴──┐
                                                                    ▼                      ▼
                                                            yt-dlp (search)          mpv (playback)
```

### Files

```
voiceyt/
  __main__.py     CLI entry point
  config.py       load + validate config.yaml, apply defaults
  audio.py        mic capture (sounddevice), 16kHz mono float32 ring buffer
  aec.py          optional echo cancellation (§8)
  vad.py          silero-vad wrapper, emits utterance segments
  asr/
    base.py       ASRBackend interface
    whisper.py    faster-whisper
    parakeet.py   onnx-asr / NeMo
    nemotron.py   onnx streaming
    registry.py   name -> class
  models.py       download, cache, verify models
  text.py         normalization + verb matching
  matcher.py      trigger state machine
  commands.py     action handlers (play/next/stop/volume_up/volume_down)
  player.py       mpv subprocess + JSON IPC
  search.py       yt-dlp wrapper
  bench.py        compare all backends (§9, milestone 2)
config.yaml
models/
```

### Dependencies

```
sounddevice numpy pyyaml yt-dlp huggingface_hub
faster-whisper            # backend: whisper
onnx-asr onnxruntime-gpu  # backends: parakeet, nemotron
silero-vad
```

External binary: **mpv** must be installed and on PATH.

---

## 3. CLI

```
python -m voiceyt                          # run with config default backend
python -m voiceyt --backend nemotron       # override backend
python -m voiceyt --list-devices           # print audio input devices + indices
python -m voiceyt --download-models all    # or: whisper parakeet nemotron
python -m voiceyt --bench audio.wav        # run all 3 backends on one file, compare
python -m voiceyt --bench-live 60          # record 60s from mic, run all 3, compare
```

---

## 4. ASR backends

All three implement the same interface. Selected at launch. All run on GPU.

```python
class ASRBackend(Protocol):
    name: str
    streaming: bool
    def load(self, cfg) -> None: ...
    def transcribe(self, pcm: np.ndarray) -> str: ...   # batch backends
    def feed(self, pcm: np.ndarray) -> str | None: ...  # streaming backends
    def reset(self) -> None: ...
```

### whisper
`faster-whisper`, model `small` or `medium`, `compute_type="float16"`, `device="cuda"`.
Batch by design (30-second windows), so it runs per-utterance behind the VAD.
**Set `language="pt"` explicitly.** Auto-detect drifts to Spanish on short clips.

### parakeet
`nvidia/parakeet-tdt-0.6b-v3`. Covers 25 European languages with automatic language
detection, and its Portuguese training data is European Portuguese rather than Brazilian —
the correct dialect here. Prefer **onnx-asr** over NeMo: NeMo pulls in a very large
dependency tree that does not belong in a background daemon. Also batch, so also behind the VAD.

Fallback if accuracy disappoints: `yuriyvnv/parakeet-tdt-0.6b-portuguese` (community
European-Portuguese fine-tune).

### nemotron
`nvidia/nemotron-3.5-asr-streaming-0.6b`. Cache-aware FastConformer-RNNT with configurable
chunk sizes of 80/160/320/560/1120 ms, reusing cached encoder context instead of
recomputing overlapping buffers, with native punctuation and capitalization. This is the
only backend that can match the trigger **mid-sentence** instead of after the speaker
stops, so it will likely feel fastest. Use `chunk_ms: 320` as the starting point.

Community ONNX export: `codavidgarcia/nemotron-3.5-asr-streaming-onnx`, runnable with
onnxruntime.

**Check the licence before making this the default** — NVIDIA licence, not Apache/MIT.

### Model download
`python -m voiceyt --download-models all` uses `huggingface_hub.snapshot_download` into
`models/<backend>/`. On startup, if the selected backend's files are missing, print the
exact download command and exit. Never download silently at runtime.

---

## 5. Audio pipeline

- Capture 16 kHz mono float32 via `sounddevice` callback into a ring buffer. Never block
  inside the audio callback.
- `silero-vad` marks speech segments. Batch backends get one complete utterance;
  the streaming backend gets continuous chunks and uses the VAD only as a power gate.
- Feed the ASR from a worker thread, not the audio thread.

---

## 6. Commands and matching

### 6.1 Normalization (`text.py`)

ASR output is inconsistent. Run every transcript through this before matching:

1. lowercase
2. **strip accents** (`pára` → `para`, `próximo` → `proximo`) — accent output is unreliable
3. strip punctuation
4. collapse whitespace

The trigger comes back as `youtube`, `you tube`, `iutube`, `u tube`, `youtube,` and worse.
The accepted spellings are a config list, matched after normalization.

### 6.2 State machine (`matcher.py`)

```
IDLE       --trigger word seen-------------------> ARMED
ARMED      --verb seen within trigger_window_ms--> COLLECTING   (verb takes a query)
ARMED      --verb seen within trigger_window_ms--> FIRE         (verb takes no query)
ARMED      --any non-verb word-------------------> IDLE
ARMED      --trigger_window_ms elapsed-----------> IDLE
COLLECTING --silence for silence_end_ms----------> FIRE
COLLECTING --query_max_ms elapsed----------------> FIRE
COLLECTING --new trigger word--------------------> ARMED (restart)
```

Three timers, and they do different jobs:

- `trigger_window_ms` (~1500) — how long a verb is still accepted after the trigger.
- `silence_end_ms` (~800) — silence that ends a query early, so short searches fire fast.
- `query_max_ms` (~6000) — hard cap, so an unrelated remark after a pause is not searched.

If the whole sentence arrives in one utterance ("youtube passa nirvana"), no timer
elapses at all — just split after the verb and fire. The timers only matter when the
speaker pauses mid-command.

The `ARMED --any non-verb word--> IDLE` transition is important: "vi um vídeo no youtube
ontem" sees `ontem`, which is not a verb, and drops straight back to IDLE.

### 6.3 Command table (in config)

```yaml
commands:
  - action: play
    verbs: [passa, toca, poe, mete]
    takes_query: true
  - action: next
    verbs: [proximo, seguinte, outra, salta]
    takes_query: false
  - action: stop
    verbs: [para, pausa, chega]
    takes_query: false
  - action: volume_up
    verbs: ["mais alto", aumenta, "sobe o som"]
    takes_query: false
  - action: volume_down
    verbs: ["mais baixo", baixa, "baixa o som"]
    takes_query: false
```

Verbs are written **without accents** (they are matched post-normalization), matched
longest-first, and multi-word verbs must work. `action` maps to a handler registered in
`commands.py`; an unknown action is a config error at startup, not at runtime.

---

## 7. Search and playback

- Search: `yt-dlp` with `ytsearch5:<query>`, `--flat-playlist -J`. Returns IDs and titles
  without resolving stream URLs, which is fast.
- Start **one persistent mpv** at daemon startup with `--input-ipc-server=<path>`
  (`\\.\pipe\voiceyt` on Windows, `/tmp/voiceyt.sock` on Linux) plus `--idle=yes
  --no-video --really-quiet`.
- Commands over JSON IPC, one JSON object per line:
  - play: `loadlist` / `loadfile` the search results as a playlist
  - next: `{"command":["playlist-next"]}`
  - stop: `{"command":["stop"]}`
  - volume: `{"command":["add","volume",10]}` / `-10`, clamp 0–130
- Full URL resolution costs ~2–5 s per video (nsig decoding). After item 1 starts,
  pre-resolve item 2 in a background thread so `próximo` feels instant.
- Keep mpv alive between commands. Restart it only if the IPC socket dies.
- Expect YouTube bot checks eventually. Wire `cookies_from_browser` in from the start.

---

## 8. Echo cancellation

**Default: keep listening while music plays.** Do not gate the recogniser — gating would
make `youtube pára` impossible, which defeats the point.

The consequence is real and must be handled: on speakers, the mic hears the music and the
ASR transcribes it. Lyrics will eventually produce a false trigger.

Order of implementation:

1. **OS-level AEC first — zero code.** On Linux, load the PipeWire/PulseAudio
   `echo-cancel` module and point `audio.device` at the cleaned source. On Windows, enable
   the capture device's built-in signal enhancement / "voice communication" mode. If
   headphones are used, the problem disappears entirely. Try this before writing anything.
2. **In-process AEC** if step 1 is insufficient. Implement `aec.py` behind
   `aec.enabled: true`, with a `backend` option:
   - `echoff` (PyPI) — synchronizes system-audio loopback with mic capture and feeds
     matched 10 ms frame pairs to WebRTC's Audio Processing Module. Windows live capture
     uses WASAPI loopback via PyAudioWPatch. **It is alpha and its live capture is Windows
     only**, so treat it as best-effort, keep it optional, and make the daemon still run
     with `aec.enabled: false`.
   - `speexdsp` (`speexdsp-python`, needs `libspeexdsp-dev`) — older, stable, Linux-friendly
     Speex echo canceller. Requires you to supply the far-end reference yourself.
   - `voiceclean` (PyPI) — pure Python + numpy AEC and VAD, no C libraries; newest and
     least proven of the three, but the easiest to install.
   All of these need the **playback reference signal**, not just the mic. Capture it from
   the system loopback device. AEC needs both streams aligned frame-by-frame; misalignment
   makes it useless, which is why `echoff` handles the timestamp alignment for you.
3. **Cheap safety net regardless of AEC**: ignore a trigger if it arrives within ~500 ms of
   playback starting, and require the trigger and verb to appear in the *same* utterance
   while audio is playing. Costs nothing and kills most music-induced false fires.

---

## 9. Milestones

Each milestone has an acceptance test. Do not move on until it passes.

**M1 — Skeleton.** Config loading, mic capture, `--list-devices`.
*Accept:* prints input devices; prints a live RMS level that reacts to speech.

**M2 — All three backends + comparison.** Implement all three ASR backends and the model
downloader now, not later. Add `--bench` / `--bench-live`: record or load one audio file,
run it through all three, and print for each one: transcript, load time, wall-clock
transcription time, and real-time factor.
*Accept:* `python -m voiceyt --bench-live 60` prints three transcripts side by side of the
same 60 seconds of Portuguese speech. Whichever wins on accuracy of the word "youtube"
and on latency becomes `asr.backend` in config.
*Also:* run the daemon with `log_transcripts: true` for a few hours of ordinary talking and
keep the log. That file is the test set for M3.

**M3 — Matcher.** Normalization, state machine, verb table.
*Accept:* replay the M2 transcript log through the matcher offline. Zero commands fire on
ordinary conversation, and every deliberate test phrase fires correctly.

**M4 — Player.** mpv IPC, yt-dlp search, play/next/stop/volume_up/volume_down.
*Accept:* all six commands work end to end, spoken into the mic, with headphones on.

**M5 — Echo cancellation.** §8 step 1, then step 2 only if needed, plus step 3 always.
*Accept:* music plays through speakers for 10 minutes with zero false triggers, and
`youtube pára` still works over the music.


---

## 10. config.yaml

```yaml
audio:
  device: null            # null = system default; --list-devices for the index
  sample_rate: 16000

aec:
  enabled: false          # start false; see plan §8
  backend: echoff         # echoff | speexdsp | voiceclean
  loopback_device: null

asr:
  backend: nemotron       # set from --bench results; --backend overrides
  models_dir: ./models
  device: cuda
  language: pt
  whisper:
    model: small
    compute_type: float16
  parakeet:
    model_id: nvidia/parakeet-tdt-0.6b-v3
  nemotron:
    model_id: nvidia/nemotron-3.5-asr-streaming-0.6b
    chunk_ms: 320

vad:
  enabled: true
  threshold: 0.5
  min_silence_ms: 400

trigger:
  words: [youtube, "you tube", iutube, "u tube", utube]
  trigger_window_ms: 1500
  silence_end_ms: 800
  query_max_ms: 6000

player:
  mpv_path: mpv
  ipc_path: null          # auto per OS
  audio_only: true
  volume_step: 10
  extra_args: ["--no-video", "--really-quiet", "--idle=yes"]

search:
  results: 5              # size of the "próximo" pool
  cookies_from_browser: null   # e.g. firefox, if bot checks start

behaviour:
  log_transcripts: true
  log_path: ./logs/transcripts.log
  ignore_trigger_ms_after_play: 500

commands:
  - {action: play,        verbs: [passa, toca, poe, mete],              takes_query: true}
  - {action: next,        verbs: [proximo, seguinte, outra, salta],     takes_query: false}
  - {action: stop,        verbs: [para, pausa, chega],                  takes_query: false}
  - {action: volume_up,   verbs: ["mais alto", aumenta, "sobe o som"],  takes_query: false}
  - {action: volume_down, verbs: ["mais baixo", baixa, "baixa o som"],  takes_query: false}
```

---

## 11. Known risks

- **False positives are the main failure mode.** M2's transcript log is the only way to
  measure the real rate. Build M3 against that log, not against guesses.
- **Mic quality matters more than model choice.** A cheap mic across a room defeats any model.
- **`echoff` is alpha and Windows-only for live capture.** AEC must stay optional.
- **yt-dlp breaks periodically** when YouTube changes. Pin a version, plan to update it.
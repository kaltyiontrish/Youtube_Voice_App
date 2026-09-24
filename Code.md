# Code.md â€” Codebase Analysis Reference for Developers

This document describes the current `voiceyt` implementation for developers and AI assistants. It is a technical map of the repository, not a replacement for `README.md` or `instalation.MD`.

## 1. Overview

`voiceyt` is a Windows-focused, voice-controlled YouTube music player. It continuously captures microphone audio, segments speech with VAD, transcribes it with a selected ASR backend, matches configured voice commands, and controls a persistent `mpv` process.

The application is designed around:

- No web server and no LLM dependency.
- Configuration-driven commands and timings.
- Explicit model downloads rather than runtime downloads.
- One persistent mpv process with a shared playback queue.
- A Tkinter overlay and raw-ctypes Windows tray icon.
- Optional NVIDIA/CUDA acceleration.
- Headless and offline diagnostic modes.
- A transcript journal for tuning and replay testing.

The trigger word alone does nothing. A configured trigger must be followed by a recognized verb inside the trigger window. While playback is active, the trigger and verb are required to come from the same utterance to reduce false triggers from music or lyrics.

## 2. Repository layout

```text
Youtube_Voice_App/
â”œâ”€â”€ voiceyt/                    Main Python package
â”‚   â”œâ”€â”€ __init__.py             Package version
â”‚   â”œâ”€â”€ __main__.py             CLI entry point, Listener, daemon run loop
â”‚   â”œâ”€â”€ config.py               YAML loading, defaults, validation, atomic save
â”‚   â”œâ”€â”€ audio.py                Microphone capture and device selection
â”‚   â”œâ”€â”€ vad.py                  Silero VAD and utterance segmentation
â”‚   â”œâ”€â”€ aec.py                  Optional echo cancellation
â”‚   â”œâ”€â”€ asr/                    ASR backend implementations
â”‚   â”‚   â”œâ”€â”€ base.py             Backend protocol/base contract
â”‚   â”‚   â”œâ”€â”€ registry.py         Backend name -> implementation
â”‚   â”‚   â”œâ”€â”€ whisper.py          faster-whisper/CTranslate2 backend
â”‚   â”‚   â”œâ”€â”€ parakeet.py         ONNX Parakeet backend
â”‚   â”‚   â”œâ”€â”€ nemotron.py         Streaming Nemotron backend
â”‚   â”‚   â””â”€â”€ vosk.py              Offline Vosk backend
â”‚   â”œâ”€â”€ vendor/                 Vendored Nemotron streaming engine
â”‚   â”œâ”€â”€ matcher.py              Trigger/verb state machine
â”‚   â”œâ”€â”€ text.py                 Normalization and phrase matching
â”‚   â”œâ”€â”€ commands.py             Voice/UI command dispatch and handlers
â”‚   â”œâ”€â”€ search.py               yt-dlp search and stream resolution
â”‚   â”œâ”€â”€ player.py               Persistent mpv JSON IPC wrapper
â”‚   â”œâ”€â”€ playlists.py            Atomic JSON playlist storage
â”‚   â”œâ”€â”€ models.py               Explicit model download and verification
â”‚   â”œâ”€â”€ transcripts.py          Journal writing, parsing, and replay
â”‚   â”œâ”€â”€ bench.py                ASR benchmark harness
â”‚   â”œâ”€â”€ dlls.py                 CUDA/cuDNN DLL path bootstrap
â”‚   â”œâ”€â”€ winjob.py               Windows kill-on-close job object
â”‚   â”œâ”€â”€ ui.py                   Tkinter overlay, Settings, tray icon
â”‚   â”œâ”€â”€ ui_theme.py             Five semantic color themes
â”‚   â””â”€â”€ ...
â”œâ”€â”€ tests/                      Offline unittest suite
â”œâ”€â”€ config.yaml                 Runtime configuration
â”œâ”€â”€ voiceyt.bat                 Windows launcher
â”œâ”€â”€ instalation.MD              Installation guide
â”œâ”€â”€ UI_Design.MD                UI design and task history
â”œâ”€â”€ README.md                   User-facing documentation
â””â”€â”€ plan2.md                    Original daemon design reference
```

Generated/local directories such as `models/`, `logs/`, `__pycache__/`, and `.venv/` are runtime data, not application source.

## 3. Entry points and commands

The normal entry point is:

```powershell
python -m voiceyt
```

`voiceyt/__main__.py` owns:

- `build_parser()` for CLI arguments.
- `main()` for configuration loading and mode selection.
- `run_daemon()` for the live microphone/listener/player loop.
- `cmd_list_devices()` for input-device discovery.
- `cmd_meter()` for a live RMS meter.
- `cmd_listen()` for transcript-only recording.
- `cmd_download_models()` for explicit model installation.
- `cmd_bench()` and `cmd_bench_live()` for ASR comparison.
- `cmd_replay_log()` for offline matcher validation.
- `cmd_aec_probe()` for echo-cancellation diagnostics.

Useful commands:

```powershell
python -m voiceyt --list-devices
python -m voiceyt --meter
python -m voiceyt --listen
python -m voiceyt --download-models whisper vad
python -m voiceyt --bench logs\\sample.wav
python -m voiceyt --bench-live 60
python -m voiceyt --replay-log logs\\transcripts.log
python -m voiceyt --aec-probe
```

On Windows, `voiceyt.bat` changes to the project directory and invokes the project virtual environment. It does not create the virtual environment or download models automatically.


## 4. Runtime data flow

The live path is:

```text
microphone
  -> sounddevice capture (16 kHz mono float32)
  -> bounded audio queue / recent ring buffer
  -> Silero VAD windows
  -> UtteranceSegmenter
  -> selected ASR backend
  -> transcript journal
  -> Matcher (trigger + verb state machine)
  -> CommandRunner
  -> Searcher (yt-dlp) and MpvPlayer (mpv JSON IPC)
  -> UiState mailbox
  -> Tkinter overlay and tray feedback
```

Important boundaries:

- Audio capture is independent of ASR and command execution.
- The daemon does not download models during normal startup.
- yt-dlp searches return canonical YouTube watch URLs first; direct stream URLs are resolved lazily.
- mpv receives resolved stream URLs plus required HTTP headers.
- UI workers never manipulate Tk widgets directly; only the Tk thread does.
- Command handlers are serialized so voice and UI operations cannot race.

## 5. Audio and speech processing

### `audio.py`

Provides microphone discovery and capture. The project targets 16 kHz mono audio for the ASR/VAD pipeline. Device selection can use the system default, a numeric PortAudio index, or a configured name substring.

### `vad.py`

Loads the configured Silero ONNX model when enabled. It identifies speech windows and emits complete utterances using configured thresholds and timing limits. The VAD is a trust boundary for command timing; malformed or missing model files should produce a clear startup error rather than silently disabling speech recognition.

### `asr/`

The registry selects the configured backend. Each backend follows the same basic contract:

```text
load model
transcribe complete audio window
reset streaming state when required
close resources
```

Backend differences:

| Backend | Type | Notes |
|---|---|---|
| Whisper | Batch | faster-whisper/CTranslate2; `float16` on CUDA |
| Parakeet | Batch | ONNX ASR; multilingual/audio-oriented option |
| Nemotron | Streaming | Vendored ONNX streaming implementation; largest model |
| Vosk | Batch | Small offline English/Portuguese models; CPU-friendly |

### `dlls.py` and CUDA

CUDA/cuDNN DLL directories from pip-installed NVIDIA wheels are added to the process DLL search path before backend imports. This avoids requiring a system CUDA toolkit. If CUDA setup is not needed, set `asr.device: cpu` and use the appropriate CPU environment.

## 6. Command matching and execution

### `matcher.py`

The state machine is:

```text
IDLE -> ARMED -> COLLECTING -> command
```

- `IDLE`: no trigger is active.
- `ARMED`: a trigger word was heard and a verb is expected.
- `COLLECTING`: a query-taking verb was heard and the query is being collected.
- A completed transcript or timer/silence boundary fires a `Command`.

Only final utterances feed the matcher. Partial streaming hypotheses must not fire commands or repeatedly trigger playback.

### `text.py`

Normalization lowercases, strips accents, removes punctuation, collapses whitespace, tokenizes phrases, and matches longest configured phrases first.

### `commands.py`

`HANDLERS` is the action registry. `validate_actions()` checks every configured action at startup. `CommandRunner` serializes voice and UI operations and handles `play`, `add`, `remove`, `jump`, `next`, `prev`, `pause`, `stop`, `resume`, `volume_up`, and `volume_down`.

`play` can accept a search query. `jump` accepts a 1-based saved-playlist position. `add` and `remove` operate on the current canonical track.

## 7. Search, resolution, and playback

### `search.py`

`Searcher` is the only yt-dlp facade used by command and UI code. Search is flat and fast:

```text
ytsearch10:<query> --flat-playlist
```

It returns `SearchResult` values containing video IDs, titles, canonical watch URLs, and optional duration. `resolve()` converts a canonical URL into a direct playable `ResolvedStream` and captures required HTTP headers.

Deno is detected from `PATH` and passed to yt-dlp as its JavaScript runtime. Current YouTube extraction may fail or return incomplete formats when Deno and `yt-dlp-ejs` are missing.

### `player.py`

`MpvPlayer` owns one persistent mpv process. It uses a Windows named pipe by default and two JSON IPC connections:

- a command connection for synchronous requests and replies;
- an event connection owned by a reader thread.

The separate event connection avoids the Windows deadlock caused by sharing one blocking mpv IPC handle between command writes and event reads.

Important player responsibilities:

- start/restart mpv safely;
- load and append resolved streams;
- preload subsequent queue items in the background;
- map mpv queue indices to canonical titles/URLs;
- navigate next/previous/jump;
- pause, resume, stop, volume, and seek;
- remember stopped queue index and playback time;
- kill mpv with the parent through a Windows job object.

Search results and saved playlists use the same `play_results(..., start_index)` path. A playlist double-click loads the complete queue and starts at the selected 1-based UI position.

## 8. Persistence and playlists

### `transcripts.py`

Transcript records are tab-separated:

```text
timestamp<TAB>backend<TAB>tag<TAB>text
```

Tags include `final`, `partial`, and `command`. Only `final` transcripts feed the matcher during replay. Command notes reproduce playback state so the same-utterance anti-lyrics guard can be tested offline.

### `playlists.py`

`PlaylistStore` owns the local JSON playlist file. It supports names, create/rename/delete, add/remove, track retrieval, and atomic replacement. Canonical YouTube URLs are stored instead of temporary googlevideo stream URLs.

A corrupt playlist should be backed up and replaced with an empty safe state rather than crashing the daemon. User playlists are local data and should not be committed by default.

### UI state

`UiState` persists selected non-sensitive UI state in `logs/ui_state.json`, including:

- saved volume;
- browse query and results;
- voice preview titles and URLs;
- recent voice queries.

Writes are temporary-file plus atomic replace. The file is local state, not a model or source file.

## 9. User interface

### `ui.py`

The UI has two modes:

- `compact`: small overlay for passive feedback;
- `expanded`: full desktop-style music interface.

The expanded shell contains:

- top voice header and microphone gain meter;
- five voice-search preview results;
- Playlists workspace;
- Browse music list with up to 10 results and a scrollbar;
- saved playlist list with a scrollbar and drag reorder;
- persistent bottom playback controls;
- theme-aware Settings;
## 10. Configuration model

`config.py` is the single source of truth for runtime configuration. It uses:

- a `DEFAULTS` dictionary for partial/older configuration files;
- frozen dataclasses for typed runtime configuration;
- deep merging of user YAML;
- path resolution relative to the configuration file;
- validation before audio/model startup;
- atomic section saving for the Settings UI.

Important sections:

```yaml
audio:      microphone and sample rate
aec:        optional echo cancellation
asr:        backend, device, model, language
vad:        Silero model and timing
trigger:    trigger words and command windows
player:     mpv, volume, queue, playlist path
search:     result count, cookies, timeout
behaviour:  transcript journal, guards, UI flag
commands:   action, verbs, query behavior
ui:         overlay geometry, behavior, theme
```

Configuration validation must reject:

- unknown command actions;
- empty or duplicate verbs;
- contradictory `takes_query` settings;
- invalid numeric ranges;
- invalid theme names;
- missing or malformed YAML.

The Settings UI writes only its edited sections through `save_config_sections()` and restores the previous file if validation or writing fails.

## 11. Testing and development

The project uses standard-library `unittest`, not a new test framework:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests are designed to be offline where possible. They use fake players, captures, listeners, temporary configuration files, and temporary playlist/state files. No microphone, model, GPU, mpv, or display should be required for the normal test suite.

Test areas include:

- `test_config.py`: defaults, path resolution, validation, atomic save;
- `test_matcher.py`: trigger timing, query collection, guards;
- `test_text.py`: normalization, phrase matching, transliteration;
- `test_commands.py`: command registry, query/playlist actions, serialization;
- `test_search.py`: search query construction and result conversion;
- `test_playlists.py`: JSON storage, corruption recovery, CRUD, ordering;
- `test_replay.py`: transcript parsing and offline matcher replay;
- `test_ui.py`: state mailbox, tray IDs, callbacks, list/player helpers;
- `test_ui_theme.py`: five themes, semantic color keys, fallback;
- `test_vosk.py`: model archive validation and extraction safety.

Before committing a broad change, run:

```powershell
.\.venv\Scripts\python.exe -m py_compile voiceyt\__main__.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
```

Tkinter widgets, the Windows tray icon, microphone capture, model loading, YouTube extraction, and mpv behavior still require manual Windows acceptance testing.

## 12. Extension points

### Add a new voice phrase

Usually edit only `config.yaml`:
```yaml
commands:
  - action: play
    verbs:
      - play
      - passa
```

The action must already exist in `commands.HANDLERS`, and `takes_query` must match the handler contract.

### Add a new command action

1. Add a handler in `commands.py` using `@handler("action")`.
2. Add the action to `config.py` defaults and shipped configuration.
3. Add matcher/config/command tests.
4. Add a Settings command-row test if the UI exposes all known actions.
5. Update this file and `UI_Design.MD`.

### Add an ASR backend

1. Implement the `ASRBackend` contract in `voiceyt/asr/`.
2. Register it in `asr/registry.py`.
3. Add backend-specific config and validation.
4. Add download/model support in `models.py`.
5. Add model and transcript tests.
6. Add CLI/docs/bench support where applicable.

### Add a new player control

1. Implement the control on `MpvPlayer`.
2. Route the control through `CommandRunner` for voice/UI serialization.
3. Add a focused test with `FakePlayer` or an equivalent fake.
4. Update the UI, Settings, command reference, and this document.



## 13. Safety and operational rules

- Never commit `.venv`, model files, logs, transcripts, browser cookies, or GitHub tokens.
- Do not add a token to `config.yaml`, source, scripts, or documentation.
- Do not enable browser cookies without explaining that yt-dlp reads authenticated browser data.
- Keep `behaviour.log_transcripts` configurable because transcripts may contain private speech.
- Do not use shell command strings for subprocesses when an argument list is sufficient.
- Keep the mpv process kill-on-close safety net.
- Do not call Tk methods from worker threads.
- Do not call network/model resolution on the Tk event thread.
- Keep model downloads explicit and verify downloaded files before activation.
- Preserve atomic writes for configuration, playlists, and UI state.
- Handle corrupt local files with a clear recovery path.

## 14. Current implementation status

The repository includes:

- Whisper, Parakeet, Nemotron, and Vosk backend integrations;
- explicit model downloading;
- transcript journaling and replay;
- mpv queue playback with playlist start positions;
- saved playlists;
- voice/UI commands including play, add, remove, jump, pause, continue, and volume;
- compact and expanded UI modes;
- persistent volume, browse results, and voice previews;
- a Windows tray and full daemon cleanup path.

The UI is still a Tkinter-based application. Modernization should preserve the existing threading, persistence, and player contracts rather than replacing them with a new GUI framework without a separate migration plan.

## 15. Documentation map

- `README.md`: user-facing behavior, installation summary, commands, acceptance tests.
- `instalation.MD`: complete Windows installation and transfer guide.
- `UI_Design.MD`: visual/UI decisions and implementation checklist.
- `plan2.md`: original daemon and design rationale.
- `NOTICE`: third-party notices and model licensing.
- `config.yaml`: active runtime configuration.

When behavior, configuration, commands, or UI changes, update this file and the relevant user-facing documentation in the same change.

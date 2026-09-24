# Final_Refactor.MD — Final Refactor and Modern UI Design

## Purpose

This is the distilled implementation plan for `voiceyt`, combining the useful decisions from `refactor_V1.MD`–`refactor_V100.MD`. It preserves the current Windows daemon, voice commands, compact overlay, expanded player, playlists, search, ASR backends, mpv, tray, Settings, themes, transcripts, and headless CLI modes.

Historical version documents remain planning history. This document is the current source of truth for the refactor.

## Non-negotiable outcomes

- No current voice command, UI mode, CLI mode, config key, playlist feature, theme, or persistence field is removed.
- Final transcripts alone drive the matcher; partial ASR results never fire commands.
- Canonical YouTube watch URLs are persisted; temporary media URLs are never saved.
- Voice and UI actions use one serialized dispatch path.
- Tkinter widgets are touched only by the Tk thread.
- Search, yt-dlp resolution, model work, and blocking mpv IPC never run on the Tk event thread.
- Configuration, playlist, and UI-state writes are atomic and recoverable.
- X, tray Quit, Ctrl-C, and signals converge on one bounded shutdown lifecycle.
- `behaviour.ui: false` and all headless modes remain functional.
- No new dependency is introduced without an explicit reason, compatibility review, and rollback plan.

## Target architecture

```text
voiceyt/
├── domain.py                 Track, PlaybackState, request/event/snapshot models
├── protocols.py              SearchPort, PlayerPort, PlaylistPort, ASRPort
├── runtime.py                bootstrap, ordered startup, run, shutdown
├── listener.py               audio -> VAD -> ASR -> matcher orchestration
├── playback.py               queue, generations, preload, play/pause/stop/seek
├── commands.py               intent registry and serialized dispatch
├── player.py                 low-level mpv process/JSON IPC adapter
├── search.py                 yt-dlp search/resolve adapter
├── config.py                 defaults, validation, immutable Config, atomic save
├── playlists.py              atomic local playlist storage
├── transcripts.py            append-only journal and offline replay
├── models.py                 explicit model download/readiness
├── ui/
│   ├── state.py              UiState, snapshots, bounded local state store
│   ├── jobs.py               cancellable workers and ordered mailbox
│   ├── theme.py              pure semantic palettes and live application
│   ├── widgets.py            shared themed controls
│   ├── voice.py              header, gain meter, transcript, voice results
│   ├── music.py              unified Music workspace and three lists
│   ├── player.py             persistent bottom player and timeline/volume
│   ├── settings.py           grouped Settings view
│   ├── compact.py            compact overlay
│   ├── expanded.py           Music/Settings shell
│   ├── tray.py               Windows tray adapter
│   └── controller.py         Tk lifecycle, one poll timer, teardown
└── ui.py                     temporary compatibility facade
```

Do not create empty layers. Keep an existing clear module when splitting would add no ownership clarity.

## Domain and playback contract

Use frozen, validated models for:

```text
Track, PlaybackState, PlaybackRequest, PlaybackEvent,
PlaybackSnapshot, TrackView, Intent, DispatchResult, UiSnapshot
```

Playback states are explicit:

```text
IDLE, LOADING, PLAYING, PAUSED, STOPPED, FAILED, ENDED
```

Queue rules:

- voice, browse, recent, and saved playlists publish canonical `Track` tuples through one playback service;
- a request has a monotonic request ID and generation;
- new queues supersede old preloads/events;
- stale results are discarded;
- current index is always valid for the published queue;
- `play` from a playlist starts the whole playlist at the selected row;
- `next`, `previous`, and `jump` operate on the same queue;
- Pause/Continue preserves position; Stop remembers index/time; Continue restores it;
- failed resolution marks the item failed without shifting metadata.

## Runtime and threading

Runtime owns the composition root and only runtime/bootstrap creates concrete adapters. Headless modes build only the dependencies they need.

Thread ownership:

- main daemon thread: lifecycle and capture orchestration;
- ASR worker: inference only;
- mpv reader: event stream only;
- search/preload workers: provider work only;
- UI workers: plain immutable results only;
- Tk thread: all widget access;
- tray thread: Win32 message loop only.

Startup failure cleans already-created resources in reverse order. Shutdown is idempotent, bounded, and ordered:

```text
stop new work -> cancel jobs -> close Tk loop -> stop tray -> stop capture/VAD
-> flush/close journal -> close ASR -> close mpv -> join workers
```

## Audio, VAD, ASR, and matching

- Audio is bounded mono float32 capture at the configured rate.
- VAD produces complete utterances; it never decides commands.
- Pause drops pending audio and clears utterance state.
- Resume resets VAD/ASR exactly once.
- Batch backends transcribe complete windows; streaming partials are diagnostic only.
- ASR backends expose `load`, `transcribe`/`feed`, `reset`, and idempotent `close`.
- Normalization is pure: lowercase, accent folding, punctuation removal, whitespace collapse, longest phrase first.
- Matcher states remain `IDLE -> ARMED -> COLLECTING -> FIRE`.
- Trigger-only speech and ordinary conversation remain no-ops.
- Existing action names and aliases remain valid.

## Search and provider boundary

`Searcher` is the only yt-dlp adapter:

- preserve the user’s exact query after trimming; do not append restrictive suffixes;
- return flat canonical search results first;
- resolve only the current/selected item and bounded approved preloads;
- keep direct stream URLs ephemeral;
- detect Deno once and report missing runtime clearly;
- use one bounded retry at most;
- preserve the current queue on search failure;
- map provider errors to safe user messages and technical logs.

## UI/UX design

### Shell

Expanded UI:

```text
voice header
sidebar: Music | Settings
unified Music workspace
persistent bottom player
```

There is no separate Player page. Compact mode remains a lightweight voice/status overlay and does not construct expanded pages or playlist/search services.

### Header

The area above the voice gain meter is the only window drag region. It contains:

- close X;
- status dot;
- recognized transcript/prompt;
- gain meter;
- short status/error line.

Voice results begin below the gain meter. The gain meter reacts to bounded RMS and never moves the window.

### Music workspace

One workspace contains:

1. **Voice Search Results:** latest five voice results, canonical URLs, double-click plays;
2. **Browse Music:** exact user search, up to configured results, scrollable, Search never auto-plays, double-click plays;
3. **Saved Playlist:** scrollable tracks, double-click starts the full queue, drag reorder persists.

The three lists share the current playback queue but maintain independent selections. Current playback, user selection, loading, failed, and focus states are visually distinct. List refresh preserves scroll, selection by canonical ID, current marker, and focus.

### Persistent player

```text
current title/source                         queue/next status
elapsed time [0–100% seek timeline] duration
previous play/pause next stop   volume −  %  +  mute
```

The timeline uses a dedicated gesture surface:

- click maps directly to position;
- drag updates local feedback;
- release commits absolute seek;
- polling never overwrites an active drag;
- unknown duration disables seeking;
- latest seek wins.

Volume buttons call the player service directly, update the label immediately, and never pass through voice matching. Play/Pause always reflects authoritative playback state. The persistent player never controls microphone listening; tray pause remains the listening control.

### Settings

Group controls as:

```text
General
Voice
Playback
Music Search
Appearance
Commands
Advanced
```

Use readable labels/descriptions and a `SettingsDraft`. Mark fields as live-safe or restart-required. Save validates, atomically writes, reloads, rolls back on failure, and applies only live-safe values. `Ctrl+S` saves, `Escape` closes/reloads safely, and `F6` moves between sections. The command editor shows registered actions, verbs, `takes_query`, validation, and restart status.

### Themes

Keep five pure semantic themes:

```text
midnight, ocean, ember, mono, rose
```

Every theme defines background, surfaces, text, muted text, accent, accent-dim, hover, pressed, selection, focus, border, danger, warning, success, and disabled. Theme switching must reapply selected navigation, current row, focus, hover/active, scrollbar, timeline, dialog, and error states. Unknown themes fall back safely to Midnight.

### Accessibility and UX

- visible focus;
- keyboard row navigation and Return activation;
- Ctrl+A Add, Delete remove, Ctrl+Up/Down reorder;
- F6 region movement;
- accessible labels/tooltips for icon controls;
- text equivalents for color-only states;
## Persistence and privacy

Owners remain singular:

```text
config.py       config.yaml
playlists.py    playlists.json
ui/state.py     logs/ui_state.json
transcripts.py  append-only journal
models.py       model files
```

All user-data writes use temporary-file plus atomic replacement. Corrupt files are backed up and replaced with safe empty/default state. Old config, playlist, UI-state, and transcript formats remain readable. Do not persist raw transcripts, tokens, cookies, direct media URLs, widgets, or worker objects. Transcript logging remains configurable.

## Security and dependency boundaries

- safe YAML loader;
- no unsafe dynamic execution;
- no `shell=True` subprocesses;
- subprocess arguments and timeout/terminate/kill policy;
- bounded model/archive/state sizes;
- reject archive traversal, absolute paths, and links;
- no secrets in source/docs/config/logs;
- browser cookies opt-in only;
- no new dependency without approval.

## Implementation packages

Execute in order, with one rollback checkpoint per package:

```text
W0  baseline and characterization tests
W1  domain models and URL identity
W2  minimal protocols and deterministic fakes
W3  runtime bootstrap and bounded cleanup
W4  search adapter contract
W5  low-level mpv adapter contract
W6  playback service and queue
W7  command intent migration
W8  listener/audio/VAD/ASR pipeline
W9  UI state and snapshots
W10 worker jobs and mailbox
W11 themes and Settings services
W12 shared widgets and voice header
W13 compact and expanded shells
W14 unified Music workspace
W15 persistent player and Settings views
W16 tray/controller and unified shutdown
W17 evidence-driven cleanup and documentation
```

Do not start a package before its dependencies are complete. Do not combine unrelated refactor, feature, dependency, or formatting work.

## Required checks

Every package runs:

```powershell
.\.venv\Scripts\python.exe -m py_compile voiceyt\__main__.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Also run focused tests for the changed boundary. UI packages require compact/expanded smoke tests and manual scaling checks when a Windows display is available.

Package reports record:

```text
Package:
Changed files:
Behavior preserved:
Focused tests:
Full suite:
Manual checks:
Risks/deferred work:
Rollback commit:
```

## Release gates

Do not release until:

- all current commands, config keys, CLI modes, themes, playlists, and UI modes work;
- compact/headless modes start without expanded dependencies;
- voice and UI commands serialize;
- all three lists activate/double-click correctly;
- playlist navigation and drag reorder work;
- stop/continue/timeline/volume semantics work;
- Settings save/reload/rollback works;
- five themes preserve all semantic states;
- no Tk worker access or close-race error exists;
- no mpv/Python orphan process exists;
- corrupt data recovery is verified;
- full tests, compile/import, pip, and diff checks pass;
- Windows manual acceptance is complete or explicitly marked unavailable;
- `Code.md`, `README.md`, `UI_Design.MD`, and `instalation.MD` match the final code.

## Final verdict format

```text
Verdict: RELEASE | HOLD
Branch/commit:
Packages completed:
Files changed:
Public compatibility preserved:
Tests/evidence:
Manual checks:
Data migrations/backups:
Security/performance checks:
Known limitations:
Deferred work:
Rollback point:
```

## Definition of done

The refactor is complete when ownership is clear, the UI is one coherent modern music workspace with reliable controls, all current functionality remains intact, data is safe, slow work never blocks Tk, playback state is explicit, and every release gate has evidence.

## 12. Current refactor status

The staged refactor packages are complete through W17. The historical `refactor_V*.MD` files remain planning records; they are not additional implementation tasks.

Final automated evidence:

```text
Python compilation: passed
Offline unittest suite: 183 passed
pip check: no broken requirements
git diff --check: passed
```

Manual Windows acceptance is still required for the live microphone, mpv, tray, overlay, Deno/yt-dlp extraction, and display scaling. Keep the release on HOLD until that checklist is completed and the working tree is reviewed/committed.




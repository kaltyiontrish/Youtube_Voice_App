# UI_Design.MD — voiceyt full-window UI (design + task list)

> Design doc + build checklist for the `ui-enhancements` branch. Read top to
> bottom, tick boxes as you go. Companion docs: `plan2.md` (daemon),
> `README.md` §"UI: overlay + tray icon" (current behaviour).

## 0. Where we stand (do not rebuild this)

Reuse everything below — the ladder says it already exists:

- `voiceyt/cli_app.py`: parser, logging setup, configuration loading, and CLI mode dispatch.
- `voiceyt/cli.py`: non-daemon commands (devices, models, benchmarks, meter, replay, AEC).
- `voiceyt/daemon.py`: daemon composition, matcher wiring, UI callbacks, and runtime shutdown.
- `voiceyt/listener.py`: audio → AEC → VAD → ASR callback pipeline.
- `voiceyt/playback_service.py`: serialized playback coordination and shared snapshot boundary.
- `voiceyt/runtime.py`: ordered resource registration and bounded cleanup.
- `voiceyt/ui.py`: `UiState`, overlay, Settings, compact/expanded views, and tray integration.
- `voiceyt/ui_theme.py`: five semantic color themes.
- `voiceyt/player.py`: low-level persistent mpv JSON IPC adapter.
- `voiceyt/search.py`: yt-dlp search/resolve adapter.
- `voiceyt/playlists.py`: atomic local playlist storage.
- `voiceyt/commands.py`: command registry and serialized intent dispatch.
- `voiceyt/config.py`: defaults, validation, immutable runtime config, atomic save.
- `tests/`: offline unit, contract, and UI compatibility tests.
- Constraints: stdlib `tkinter(+ttk)` + `ctypes` only, no new dependency;
  Windows tray stays Windows-only; headless modes remain functional with
  `behaviour.ui: false`.

## 1. Window & space budget (the app must fit everything)

- `compact` mode = a lightweight 400x214 voice/status overlay.
- `expanded` mode = a modern shell with a dark sidebar (`Playlists`, `Settings`),
  frameless top voice header, gain meter, one unified Music workspace, and a
  persistent bottom player.
- The three visible music surfaces are: Voice Search Results (top), Browse Music,
  and Saved Playlist tracks. There is no separate Player page or hidden player list.
- The persistent bottom bar owns transport, song position, and volume controls.
- Browse and saved playlist lists scroll independently; playlist drag/drop is
  persisted immediately. Double-click plays the selected result/track.

## 2. Tasks

### A. Groundwork — wire the config that already exists (do first)

- [x] A1 `UiConfig` dataclass + `_build()` parsing/validation for the whole `ui:`
  block; defaults stay exactly as in `config.yaml`. Files: `voiceyt/config.py`,
  `tests/test_config.py` (one case per key incl. bad width/mode rejected).
- [x] A2 `PlayerConfig.playlist_path: Path | None` + resolve relative to the config
  dir (same `Config.resolve()` pattern as `log_path`); `null` = disabled.
  Files: `voiceyt/config.py`, `tests/test_config.py`.
- [x] A3 Replace hardcoded `WINDOW_WIDTH/WINDOW_HEIGHT/WINDOW_POS/FADE_AFTER_S/
  HIDE_AFTER_S` in `voiceyt/ui.py` with the loaded `UiConfig` (keep the same
  constants as fallbacks only). `hide_while_playing` hides on `player.playing`.
  Files: `voiceyt/ui.py`, `tests/test_ui.py`.

### B. Music player tab (reuse `MpvPlayer` — no new player code)

- [x] B1 Header: status dot `UiState.snapshot()` + big heard line + live mic meter
  (move current header widgets up, unchanged logic).
- [x] B2 Now-playing line: `player.current_title()` + `[pos/total]` +
  `vol %` (reuse `Overlay._now_playing()` verbatim).
- [x] B3 Transport buttons → existing methods only: Play/Pause toggles
  `Tray.toggle_pause` semantics; Stop → `player.stop()`; Prev/Next →
  `player.prev_track()/next_track()`; Resume-after-stop keeps the documented
  replay-current-index behaviour (`player.py` "mpv's stop clears the playlist").
- [x] B4 Volume slider + mute → `set_volume()` (clamps 0–130 already inside);
  step buttons ±`volume_step`.
- [x] B5 Pool listbox: `pool_titles()` rows, click → `jump_to(index)` via the
  existing `_ui_jump_to_pool` bounds check (pool-changed-since-click = no-op).
- [x] B6 Status bar: mic name (`capture.device_name`), ASR backend, mpv alive,
  AEC state. Poll in the existing 250 ms tick — **no new threads**.
- [x] B7 Accept: `behaviour.ui: false` runs 100% headless; every `test_ui.py`
  test still passes; pool click out of range still a no-op.

### C. Playlists tab (the only new file in the project)

- [x] C1 NEW `voiceyt/playlists.py` (~80 lines, stdlib `json` only): JSON file at
  `player.playlist_path` = `{"playlists": {"name": [{"title":…, "url":…}]}}`;
  `load/save/create/rename/delete/add/remove/reorder`; atomic save
  (write-temp-then-replace); corrupt file → back up to `*.bak` and start empty,
  never crash the daemon. Reuse `path.read_text/write_text` idiom from probes.
- [x] C2 Tab UI: playlist dropdown + track list (double-click →
  `player.load(url, "replace")`); Add-current (uses `current_title()` +
  mpv `path` property); New/Rename/Delete; drag- or button-reorder.
- [x] C3 Voice hooks: zero daemon changes — `play` results remain the live pool;
  saved playlists are UI-only until a later milestone wires new
  actions/verbs through `commands.py` + `validate_actions()`.
- [x] C4 Accept: restart restores playlists; corrupt JSON boots clean with a
  `*.bak` beside it; `playlist_path: null` hides the tab.

### D. Settings tab (edit `config.yaml`, incl. voice commands)

- [x] D1 Form bound to the live `Config` dataclasses: audio device (dropdown from
  `list_mics()` + `--list-devices` indices), sample_rate display (fixed 16000 —
  show read-only; `_build()` rejects anything else), VAD threshold/silences,
  trigger words + windows, player volume min/max/step, search results/cookies,
  behaviour flags, all `ui:` keys incl. `mode: compact|expanded` (applies live,
  no restart; window resizes in place).
- [x] D2 Voice-command editor: table of the 7 actions (`play/next/prev/stop/
  resume/volume_up/volume_down` + any future ones from `HANDLERS`) with editable
  verb lists and `takes_query` checkbox. Validate on save with the **existing**
  rules: unique actions, unique verbs (case-insensitive), non-empty,
  `takes_query` bool, every action in `HANDLERS` (`validate_actions()`), and
  every `HANDLERS` entry present (warn-only, same as today). Bad edit =
  inline error, file untouched.
- [x] D3 Save = `yaml.safe_dump` round-trip of only the edited sections
  (preserve comments elsewhere is out of scope — say so in the tab); **changing
  commands requires daemon restart** (matcher is built once in `run_daemon`):
  show "restart to apply voice-command changes" banner. Other tabs keep working
  until restart.
- [x] D4 "Copy diagnostics" button: device list + backend + last error onto the
  clipboard (kills the back-and-forth of past mic-bug sessions).
- [x] D5 Accept: saving invalid verbs is refused with the message; restart
  applies them (`--replay-log` proves they fire); nothing else in the file is
  lost.

### E. Anything-that-fits (small, each optional, each one task)

- [x] E1 Recent/history strip: last 10 `play` queries from `CommandRunner`
  (`last_query/last_results` already stored) — click to replay.
- [x] E2 Search box in Player tab: type query → `searcher.search()` →
  `play_results()` (same path as the voice `play` handler; voice stays primary).
- [x] E3 Backend picker: `--backend`-equivalent dropdown (whisper/parakeet/
  nemotron/vosk) — applies on restart, same rule as D3.
- [x] E4 Desktop shortcut + autostart toggle in Settings (reuse
  `create_shortcut.ps1`; document the Startup-folder step in-app).
- [x] E5 `?` overlay: trigger word + all verbs cheat-sheet, generated from the
  live `Config` so it never goes stale.

## 3. Suggestions (my take, in build order)

1. **Do A1–A3 first, alone.** They turn 8 dead config keys live with zero visual
   change — smallest diff that unblocks everything, and each has a one-case test.
2. **Playlists before Settings.** C1–C4 deliver visible value without touching
   `config.yaml` semantics; D2/D3 (command editing) is the riskiest item because
   a bad save bricks voice control — hence validate-then-banner, never silent.
3. **Say no to:** embedded video (`audio_only: true` + `--no-video` is a deliberate
   music-player choice; video needs a new renderer, new deps, new thread), web
   UI/Electron (kills the zero-dep rule), and a custom widget theme (ttk native
   + current `#11151a` palette is enough).
4. **Later, if wanted:** per-playlist voice verbs via new `commands.py` actions
   (C3 hook), mini-player `compact` auto-switch on `hide_while_playing`, global
   hotkey for pause (win32 `RegisterHotKey` in the tray thread — same pattern as
   the icon callbacks).

## 4. Test plan (one runnable check per task, `python -m unittest`)

- A1/A2: extend `tests/test_config.py` — defaults load, each bad key raises
  `ConfigError` naming the key (run: `python -m unittest tests.test_config -v`).
- A3/B: extend `tests/test_ui.py` — `UiConfig` sizes reach `Overlay.geometry`;
  `FakePlayer` clicks/transport/volume still pass (existing file, no new file).
- C1: `tests/test_playlists.py` (only new test file): round-trip save/load,
  corrupt-file → `.bak` + empty, reorder persists.
- D2: `tests/test_config.py` — duplicate verb across actions rejected; unknown
  action rejected via `validate_actions()`; verb lowercasing preserved.
- Manual accept per tab in B7/C4/D5; keep `logs/test_all.txt` convention
  (append the run output).

## 5. Modern player revamp (approved 2026-09-23)

This section supersedes the dated top-notebook/form appearance in sections 1–4 while
keeping their daemon, command, config, and compact-overlay work intact.

### Product model

The expanded window is one desktop music player, not three independent forms. Voice
search, typed search, recent searches, and saved playlists all feed the same ordered
mpv queue. A track has a stable display title and a canonical source URL; temporary
resolved stream URLs are playback details and are never persisted.

Interaction patterns follow familiar desktop players (persistent transport, visible
queue, active-row highlighting, library sidebar), but the implementation and visual
assets remain original and stdlib-only.

### Target layout

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ voice status / transcript / live mic meter                              │
├──────────────┬───────────────────────────────────────────────────────────┤
│ Playlists    │ Browse results + saved playlists + shared queue           │
│ Settings     │ Browse search is explicit; double-click starts playback   │
├──────────────┴───────────────────────────────────────────────────────────┤
│ persistent mini status: backend · mpv · mic · error                      │
└──────────────────────────────────────────────────────────────────────────┘
```

- `compact` stays a lightweight 400×214 voice/status overlay.
- `expanded` uses a dark sidebar instead of a top `ttk.Notebook`.
- Primary colour is restrained green; elevated panels use charcoal surfaces.
- Segoe UI Variable/Segoe UI, flat controls, clear hover/selected states.
- Native Tk/ttk is sufficient; no Electron, web server, image assets, or new GUI lib.

### Revamp checklist

- [x] R1 Stable queue metadata: ordered titles + canonical URLs are installed
  before preloading; failed/stale preloads cannot corrupt the mapping.
- [x] R2 `play_results(..., start_index)` starts a whole saved playlist at the
  selected track while preserving continuous next/previous playback.
- [x] R3 Daemon callback `_ui_play_playlist` converts saved tracks to the shared
  queue path; the UI no longer calls `player.load(url, "replace")` directly.
- [x] R4 Add-current persists `MpvPlayer.current_url()` (watch URL), never mpv's
  resolved googlevideo `path`.
- [x] R5 Volume control uses explicit −/+ buttons and Mute; the bottom horizontal
  slider is reserved for 0–100% song position and is draggable without window drag.
  Volume clicks update the displayed value immediately and commit mpv asynchronously.
- [x] R6 Player Play/Pause controls mpv playback, not the microphone/listener. The
  persistent button swaps Play/Pause from the live player state; Stop remembers the
  current track position, and Continue resumes it instead of restarting at zero.
- [x] R7 Modern expanded shell: sidebar page registry, charcoal surfaces, green
  accent, compact icon transport, and active navigation state.
- [x] R8 Saved playlists and shared queue expose the same active canonical URL;
  UI polling highlights the current row.
- [x] R9 Persistent bottom mini-player controls keep transport and volume available
  on Playlists and Settings without duplicating command logic.
- [x] R10 Playlist page has themed controls, numbered rows, track counts, loading
  feedback, a right-side scrollbar beside the track list, and canonical active-row
  highlighting without playback side effects. Tracks reorder by drag-and-drop;
  Move up/down buttons are not used.
- [x] R11 Settings uses elevated section headers, readable labels, a green primary
  Save action, and the existing validated atomic save/rollback flow.
- [x] R12 Keyboard/mouse-wheel volume controls and a click-drag playback seek bar
  are complete, with IPC committed off the Tk event thread.
- [ ] R13 Add context actions: Play next / Add to queue / Add search result to a
  chosen playlist. Defer drag-sort UI until button reorder is manually accepted.
- [ ] R14 Update tests from notebook tabs to sidebar pages, then run the full
  suite plus a Windows manual acceptance pass at 100%, 125%, and 150% scaling.

- [x] R15 The Player navigation page is removed. One Playlists workspace owns
  browse results, saved playlists, voice previews, and the shared active queue;
  persistent transport/volume remain global.
- [x] R16 Expanded voice header always renders heard text, a live gain meter, and
  the five voice-command previews directly below the meter.
- [x] R17 Browse search lists up to 10 music results with a scrollbar and never starts
  playback; double-click explicitly plays the selected result through the shared queue.
  It does not replace the five voice-preview rows.
- [x] R18 Playlist and Settings comboboxes use the modern shared ttk theme.
- [x] R19 Voice `add`/`remove` operate on the current canonical track and the first
  configured playlist; numeric `youtube play N` plays playlist position N (1-based).
- [x] R20 Volume, browse query/results, voice previews, and recent queries persist in
  `logs/ui_state.json`; the X button requests full daemon shutdown.
- [x] R21 The three music surfaces are explicitly scoped: voice previews at the
  top, Browse Music in the Playlists page, and saved tracks in the playlist list.
  Generic Add uses the most recently selected surface instead of guessing.
- [x] R22 Playlist drag-and-drop has a movement threshold, a visible drop hint,
  and initializes drag state explicitly, so a normal click is not treated as a
  reorder.
- [x] R23 The persistent Play/Pause button uses authoritative mpv state after
  seek transitions, avoiding the previous two-click behavior.
- [x] R24 The song-position bar owns its gesture bindings; the header drag region
  does not intercept the slider.
- [x] R25 The persistent bar exposes Now Playing and the next queued track.
- [x] R26 All three music lists support Return/Space shortcuts, and list focus has
  visible focus styling.
- [x] R27 Empty Browse and playlist states explain the next action.
- [x] R28 Queue and playback state remain separate from the user's last selection;
  selection never implicitly starts playback.
- [x] R29 The Add button identifies the active source (Browse, Voice, or Track).
- [x] R30 Current playback is marked in all three music lists without changing
  the user's selected row.
- [x] R31 Playlist keyboard reordering, Delete, and Ctrl+A are available in
  addition to drag-and-drop.

### Settings and theme work

- [x] T1 Added five semantic themes: Midnight, Ocean, Ember, Mono, and Rose.
- [x] T2 Added validated `ui.theme` configuration with Midnight default and
  safe fallback behavior.
- [x] T3 Added live theme selection from Settings with recursive widget refresh.
- [x] T4 Added theme palette/configuration tests and kept the full suite green.
- [x] T5 Added Settings dirty-state tracking, Save/Reload state reset, and live theme
  selection alongside the existing validated save path.
- [x] T6 Added Ctrl+S, Escape, and F6 Settings bindings.
- [ ] T7 Complete focus/accessibility labels and Settings filtering.


### Invariants / acceptance

1. Double-clicking playlist track N loads every track and starts at N.
2. Next/previous traverse that same loaded queue.
3. Player and Playlists show the same active track.
4. Stop is hard; Pause/Resume keeps the position. Stop then Play/Continue
   restores the same remembered position rather than silently jumping to zero.
5. Volume uses the persistent −/+ buttons, Mute, and voice commands; the
   song-position bar is independent, click/draggable from 0–100%, and never
   fights the window drag handler.
6. Slow yt-dlp/mpv work never executes on the Tk event thread or reads Tk
   variables from a worker.
7. `behaviour.ui: false` and every headless CLI mode remain unaffected.
8. `player.playlist_path: null` hides the Playlists page.
9. Full `unittest` suite and `git diff --check` pass before commit.

### Manual Windows acceptance

- Drag volume slowly/fast to 0/65/130; click track and use wheel/arrows.
- Play a saved playlist, then use next/previous across its boundaries.
- Switch pages while playing; active row and persistent transport stay correct.
- Pause on one page, navigate, then Resume from the persistent control.
- Stop, then Play: starts the selected queue track from the beginning.
- Reorder a playlist while active; saved order and queue order agree.
- Test 100%, 125%, and 150% Windows display scaling.

---

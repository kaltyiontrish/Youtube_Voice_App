# UI_Design.MD — voiceyt full-window UI (design + task list)

> Design doc + build checklist for the `ui-enhancements` branch. Read top to
> bottom, tick boxes as you go. Companion docs: `plan2.md` (daemon),
> `README.md` §"UI: overlay + tray icon" (current behaviour).

## 0. Where we stand (do not rebuild this)

Reuse everything below — the ladder says it already exists:

- `voiceyt/ui.py` (734 lines): `UiState` mailbox (`set_heard/set_action/set_error/snapshot`,
  `paused`/`shutdown` events), `Overlay(tk.Tk)` (250 ms poll, drag-vs-click guard,
  status dot, mic meter, 5-row pool), `Tray` (raw-ctypes win32, pause/resume, mic
  submenu, quit). Threading contract: **only the tkinter thread touches widgets**.
- `voiceyt/player.py` `MpvPlayer`: `load/play_results/next_track/prev_track/jump_to/
  stop/unpause/volume/set_volume/volume_delta/pool_titles/current_title/status`
  (+ `playing` event-tracked, no IPC round trip).
- `voiceyt/commands.py`: `HANDLERS` registry + `validate_actions()` (startup check).
- `voiceyt/config.py`: `_deep_merge` defaults, frozen dataclasses, command rules
  (unique actions; unique verbs case-insensitive; non-empty; `takes_query` bool).
- `tests/test_ui.py`: unittest style with `FakePlayer/FakeCapture/FakeListener`.
- Config keys waiting to be wired: `ui:` block (`overlay_width/height`,
  `always_on_top`, `show_playlist`, `fade_after_s/hide_after_s`,
  `hide_while_playing`, `mode`) and `player.playlist_path` — **both are parsed
  nowhere today** (no `UiConfig` dataclass; `PlayerConfig` has no
  `playlist_path` field). Groundwork tasks fix that first.
- Constraints: stdlib `tkinter(+ttk)` + `ctypes` only, **no new dependency**;
  Windows tray stays Windows-only; every headless mode (`--listen`, `--bench`,
  `--replay-log`…) keeps working with `behaviour.ui: false`.

## 1. Window & space budget (the app must fit everything)

- `compact` mode = today's 400x214 single panel (status + heard + meter + 5 pool rows).
- `expanded` mode = minimum **960x540** from config (`overlay_width` 960–1920,
  `overlay_height` 540–1080), frameless + draggable (keep current drag handlers),
  `always_on_top` honoured, `ttk.Notebook` tabs on top (stdlib, no new dep).
- Pixel budget at 960x540: header 56 (dot + heard + meter) · tab bar 34 ·
  content 410 · status bar 28 (mic · backend · mpv · AEC) · padding 12 = **540**.
- Player tab at 410 px: now-playing 70 · transport 56 · volume 40 · pool list
  5×44 = 220 · gaps 24 = **410**. Pool grows by scrolling, not by growing the
  window (`ponytail:` fixed 5-row viewport + scrollbar; virtualized list is the
  upgrade path if pools ever exceed ~200 rows).

## 2. Tasks

### A. Groundwork — wire the config that already exists (do first)

- [ ] A1 `UiConfig` dataclass + `_build()` parsing/validation for the whole `ui:`
  block; defaults stay exactly as in `config.yaml`. Files: `voiceyt/config.py`,
  `tests/test_config.py` (one case per key incl. bad width/mode rejected).
- [ ] A2 `PlayerConfig.playlist_path: Path | None` + resolve relative to the config
  dir (same `Config.resolve()` pattern as `log_path`); `null` = disabled.
  Files: `voiceyt/config.py`, `tests/test_config.py`.
- [ ] A3 Replace hardcoded `WINDOW_WIDTH/WINDOW_HEIGHT/WINDOW_POS/FADE_AFTER_S/
  HIDE_AFTER_S` in `voiceyt/ui.py` with the loaded `UiConfig` (keep the same
  constants as fallbacks only). `hide_while_playing` hides on `player.playing`.
  Files: `voiceyt/ui.py`, `tests/test_ui.py`.

### B. Music player tab (reuse `MpvPlayer` — no new player code)

- [ ] B1 Header: status dot `UiState.snapshot()` + big heard line + live mic meter
  (move current header widgets up, unchanged logic).
- [ ] B2 Now-playing line: `player.current_title()` + `[pos/total]` +
  `vol %` (reuse `Overlay._now_playing()` verbatim).
- [ ] B3 Transport buttons → existing methods only: Play/Pause toggles
  `Tray.toggle_pause` semantics; Stop → `player.stop()`; Prev/Next →
  `player.prev_track()/next_track()`; Resume-after-stop keeps the documented
  replay-current-index behaviour (`player.py` "mpv's stop clears the playlist").
- [ ] B4 Volume slider + mute → `set_volume()` (clamps 0–130 already inside);
  step buttons ±`volume_step`.
- [ ] B5 Pool listbox: `pool_titles()` rows, click → `jump_to(index)` via the
  existing `_ui_jump_to_pool` bounds check (pool-changed-since-click = no-op).
- [ ] B6 Status bar: mic name (`capture.device_name`), ASR backend, mpv alive,
  AEC state. Poll in the existing 250 ms tick — **no new threads**.
- [ ] B7 Accept: `behaviour.ui: false` runs 100% headless; every `test_ui.py`
  test still passes; pool click out of range still a no-op.

### C. Playlists tab (the only new file in the project)

- [ ] C1 NEW `voiceyt/playlists.py` (~80 lines, stdlib `json` only): JSON file at
  `player.playlist_path` = `{"playlists": {"name": [{"title":…, "url":…}]}}`;
  `load/save/create/rename/delete/add/remove/reorder`; atomic save
  (write-temp-then-replace); corrupt file → back up to `*.bak` and start empty,
  never crash the daemon. Reuse `path.read_text/write_text` idiom from probes.
- [ ] C2 Tab UI: playlist dropdown + track list (double-click →
  `player.load(url, "replace")`); Add-current (uses `current_title()` +
  mpv `path` property); New/Rename/Delete; drag- or button-reorder.
- [ ] C3 Voice hooks: zero daemon changes — `play` results remain the live pool;
  saved playlists are UI-only until a later milestone wires new
  actions/verbs through `commands.py` + `validate_actions()`.
- [ ] C4 Accept: restart restores playlists; corrupt JSON boots clean with a
  `*.bak` beside it; `playlist_path: null` hides the tab.

### D. Settings tab (edit `config.yaml`, incl. voice commands)

- [ ] D1 Form bound to the live `Config` dataclasses: audio device (dropdown from
  `list_mics()` + `--list-devices` indices), sample_rate display (fixed 16000 —
  show read-only; `_build()` rejects anything else), VAD threshold/silences,
  trigger words + windows, player volume min/max/step, search results/cookies,
  behaviour flags, all `ui:` keys incl. `mode: compact|expanded` (applies live,
  no restart; window resizes in place).
- [ ] D2 Voice-command editor: table of the 7 actions (`play/next/prev/stop/
  resume/volume_up/volume_down` + any future ones from `HANDLERS`) with editable
  verb lists and `takes_query` checkbox. Validate on save with the **existing**
  rules: unique actions, unique verbs (case-insensitive), non-empty,
  `takes_query` bool, every action in `HANDLERS` (`validate_actions()`), and
  every `HANDLERS` entry present (warn-only, same as today). Bad edit =
  inline error, file untouched.
- [ ] D3 Save = `yaml.safe_dump` round-trip of only the edited sections
  (preserve comments elsewhere is out of scope — say so in the tab); **changing
  commands requires daemon restart** (matcher is built once in `run_daemon`):
  show "restart to apply voice-command changes" banner. Other tabs keep working
  until restart.
- [ ] D4 "Copy diagnostics" button: device list + backend + last error onto the
  clipboard (kills the back-and-forth of past mic-bug sessions).
- [ ] D5 Accept: saving invalid verbs is refused with the message; restart
  applies them (`--replay-log` proves they fire); nothing else in the file is
  lost.

### E. Anything-that-fits (small, each optional, each one task)

- [ ] E1 Recent/history strip: last 10 `play` queries from `CommandRunner`
  (`last_query/last_results` already stored) — click to replay.
- [ ] E2 Search box in Player tab: type query → `searcher.search()` →
  `play_results()` (same path as the voice `play` handler; voice stays primary).
- [ ] E3 Backend picker: `--backend`-equivalent dropdown (whisper/parakeet/
  nemotron/vosk) — applies on restart, same rule as D3.
- [ ] E4 Desktop shortcut + autostart toggle in Settings (reuse
  `create_shortcut.ps1`; document the Startup-folder step in-app).
- [ ] E5 `?` overlay: trigger word + all verbs cheat-sheet, generated from the
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

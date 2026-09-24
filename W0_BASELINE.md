# W0 Refactor Baseline Report

## Scope

W0 only. No production architecture, behavior, dependency, or persistence changes are made in this package.

## Characterization added

`tests/test_refactor_contract.py` freezes the current public contract for:

- CLI modes and overrides;
- registered command actions;
- exact browse query preservation;
- UI state snapshot keys and copy behavior.

## Current contract inventory

- CLI entry: `voiceyt.__main__.build_parser()` and `main()`
- Current command actions: `play`, `add`, `remove`, `jump`, `next`, `prev`, `pause`, `stop`, `resume`, `volume_up`, `volume_down`
- Browse behavior: exact query is sent to yt-dlp; search does not itself start playback
- UI state: transcript, action, error, volume, playing state, voice previews/URLs, recent searches, browse results/query
- Existing persistence owners: `config.py`, `playlists.py`, `transcripts.py`, and UI state in `ui.py`
- Existing playback owner: `player.py`; UI callbacks currently bridge to it

## Required validation

```powershell
.\.venv\Scripts\python.exe -m py_compile voiceyt\__main__.py voiceyt\commands.py voiceyt\search.py voiceyt\ui.py tests\test_refactor_contract.py
.\.venv\Scripts\python.exe -m unittest tests.test_refactor_contract -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

The current shell did not return reliable command output for the validation run; these commands must be rerun in a clean terminal before W0 is marked passed.

## Validation results

- Focused W0 tests: **4 passed**
- Full offline suite: **165 passed**
- `pip check`: **passed**
- Python compile check: pending direct rerun
- `git diff --check`: pending direct rerun

The existing suite emits expected warnings for Deno and test-only corrupt-fixture recovery. Those are not W0 failures.

W0 is not marked complete until the two pending direct checks pass.

## Rollback

W0 changes only `tests/test_refactor_contract.py` and this report. Revert the W0 commit or remove those two files if validation exposes a bad characterization assumption.

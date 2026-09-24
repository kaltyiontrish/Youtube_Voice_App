# W1 Domain Model Report

## Scope

W1 adds the isolated, dependency-free domain model boundary from `Final_Refactor.MD`. No runtime callers, UI, mpv, search, or configuration behavior was changed.

## Added

- `voiceyt/domain.py`
  - `Track` with canonical YouTube identity validation
  - `PlaybackState`
  - `PlaybackRequest`
  - `PlaybackEvent`
  - `TrackView`
  - `UiSnapshot`
  - `normalize_youtube_url()`
  - `DomainError`
- `tests/test_domain.py`

## Invariants covered

- watch, short, and youtu.be URL variants normalize to one identity
- track ID must match canonical URL
- titles/IDs are bounded and non-empty
- durations are positive when present
- request start indices are valid
- event IDs/position/state are bounded
- UI volume and microphone level are bounded
- snapshots are frozen and cannot be mutated

## Validation

- Focused W1 tests plus W0 contract tests: **8 passed**
- Full offline suite: **169 passed**
- Compile check: passed
- `git diff --check`: passed
- `pip check`: passed

The existing expected Deno and corrupt-fixture warnings remain informational.

## Rollback

Remove `voiceyt/domain.py` and `tests/test_domain.py`; no existing runtime code imports them yet.

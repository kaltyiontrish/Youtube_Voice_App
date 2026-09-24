# W2 Protocol Report

## Scope

W2 adds the minimal cross-cutting protocol seams and deterministic fakes. Runtime callers are not migrated yet.

## Added

- `voiceyt/protocols.py`
  - `SearchPort`
  - `PlayerPort`
  - `PlaylistPort`
- `tests/fakes.py`
  - deterministic `FakeSearcher`, `FakePlayer`, `FakePlaylist`
- `tests/test_protocols.py`
  - protocol conformance and call-recording tests

The existing ASR protocol in `voiceyt/asr/base.py` was reused rather than duplicated.

## Protocol rules

- protocols use domain/primitive values;
- no Tk, socket, subprocess, or provider types appear in signatures;
- no new dependency;
- no existing production module imports the new protocols yet.

## Validation

- Focused W2/W1/W0 tests: **11 passed**
- Full offline suite: **172 passed**
- Compile check: passed
- `git diff --check`: passed
- `pip check`: passed

## Rollback

Remove `voiceyt/protocols.py`, `tests/fakes.py`, and `tests/test_protocols.py`; no current production code depends on them yet.

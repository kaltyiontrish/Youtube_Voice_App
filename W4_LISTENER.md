# W4 — Listener extraction

## Status

`Listener` now lives in `voiceyt/listener.py`; `voiceyt.__main__.Listener` remains a compatibility re-export so existing callers and tests keep working.

## Responsibilities

- audio capture lifecycle
- AEC processing
- VAD segmentation
- batch/streaming ASR dispatch
- pause/flush/reset behavior
- callback delivery

The daemon remains responsible for matcher, command, UI, and resource composition.

## Validation

The existing listener pause tests exercise the moved implementation. Future W5 work may add listener-specific tests without changing the public import path.

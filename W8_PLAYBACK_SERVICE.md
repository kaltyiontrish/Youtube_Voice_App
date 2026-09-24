# W8 — Playback service boundary

## Status

Added `voiceyt/playback_service.py`. `CommandRunner` now delegates search playback, playlist playback, mpv restart, and canonical playlist conversion through the service. Command matching, guards, and UI state remain in their existing owners.

## Validation

The service has focused tests and the full suite remains the compatibility gate.

# W3 — Runtime lifecycle

## Status

Implemented the first runtime boundary: `RuntimeLifecycle` owns registered cleanup callbacks and is used by `run_daemon` for the backend, player, transcript, listener, tray, and overlay wait.

## Behavior

- Cleanup is idempotent.
- Resources are closed in reverse acquisition order, matching dependency safety.
- One failing cleanup is logged and does not block later resources.
- Registration after shutdown is rejected.
- Existing CLI modes and daemon behavior remain unchanged.

## Validation

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_runtime -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m py_compile voiceyt\__main__.py voiceyt\runtime.py
```

## Next

Wrap construction/bootstrap failures in the same lifecycle boundary before moving listener ownership out of `__main__.py`.

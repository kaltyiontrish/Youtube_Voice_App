"""Tests for bounded runtime resource cleanup."""

from __future__ import annotations

import unittest

from voiceyt.runtime import RuntimeLifecycle


class RuntimeLifecycleTests(unittest.TestCase):
    def test_cleanup_is_once_and_preserves_reverse_order(self) -> None:
        calls: list[str] = []
        runtime = RuntimeLifecycle()
        runtime.register("first", lambda: calls.append("first"))
        runtime.register("second", lambda: calls.append("second"))
        runtime.close()
        runtime.close()
        self.assertEqual(calls, ["second", "first"])
        self.assertTrue(runtime.closed)

    def test_failure_does_not_skip_later_resources(self) -> None:
        calls: list[str] = []
        runtime = RuntimeLifecycle()

        def fail() -> None:
            calls.append("failed")
            raise RuntimeError("boom")

        runtime.register("failed", fail)
        runtime.register("later", lambda: calls.append("later"))
        with self.assertLogs("voiceyt.runtime", level="ERROR"):
            runtime.close()
        self.assertEqual(calls, ["later", "failed"])

    def test_lifecycle_is_idempotent_after_failure(self) -> None:
        calls: list[str] = []
        runtime = RuntimeLifecycle()
        runtime.register("resource", lambda: calls.append("close"))
        runtime.close()
        runtime.close()
        self.assertEqual(calls, ["close"])
        self.assertTrue(runtime.closed)

    def test_registration_after_close_is_rejected(self) -> None:
        runtime = RuntimeLifecycle()
        runtime.close()
        with self.assertRaises(RuntimeError):
            runtime.register("late", lambda: None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

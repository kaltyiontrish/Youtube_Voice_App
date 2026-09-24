"""Command dispatch is serialized across voice and UI callers."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from voiceyt.commands import CommandRunner
from voiceyt.matcher import Command


class CommandRunnerTests(unittest.TestCase):
    def test_dispatch_serializes_concurrent_handlers(self) -> None:
        config = SimpleNamespace(
            behaviour=SimpleNamespace(ignore_trigger_ms_after_play=0)
        )
        player = SimpleNamespace(alive=True)
        runner = CommandRunner(config, player, SimpleNamespace())
        active = 0
        maximum = 0
        guard = threading.Lock()

        def handler(_runner, _command) -> bool:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return True

        command = Command("pause", "", 0.0, "youtube pause", "test")
        with patch("voiceyt.commands.HANDLERS", {"pause": handler}):
            threads = [
                threading.Thread(target=runner.dispatch, args=(command,))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(1.0)
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()

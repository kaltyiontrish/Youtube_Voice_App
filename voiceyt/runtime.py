"""Bounded runtime resource lifecycle for the daemon.

Resources are registered as they are acquired and released in registration order,
matching the existing voiceyt shutdown contract. A failed close is logged but
never prevents later resources from being released.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


class RuntimeLifecycle:
    """Own startup cleanup without introducing a second shutdown controller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._resources: list[tuple[str, Callable[[], Any]]] = []

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def register(self, name: str, cleanup: Callable[[], Any]) -> None:
        """Register an acquired resource; duplicate registration is allowed."""
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime lifecycle is already closed")
            self._resources.append((name, cleanup))

    def close(self) -> None:
        """Release every registered resource once, in reverse registration order.

        Later registrations are dependent UI services and are released first;
        the listener, player, log, and backend then close underneath them.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            resources = list(self._resources)
            self._resources.clear()
        for name, cleanup in reversed(resources):
            try:
                cleanup()
            except Exception:
                LOGGER.exception("runtime cleanup failed for %s", name)

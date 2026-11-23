"""Plugin-owned runtime data in ``ctx.state`` (profile-scoped JSON, never ``config.yaml``)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .mcp import SpecivoClient, SpecivoError, StatusInfo

logger = logging.getLogger(__name__)

#: How long the status list read from ``specivo_list_lookups`` is reused.
STATUS_CACHE_SECONDS = 24 * 3600


class StateStore(Protocol):
    """``ctx.state``: ``get(key, default)`` and ``set(key, value)`` of JSON values."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


#: Serialises read-modify-write of one state key inside this process (Hermes locks across processes).
STATE_LOCK = threading.RLock()


def update_state(state: StateStore, key: str, default: Any, change: Callable[[Any], Any]) -> Any:
    """Read *key*, apply *change* (which returns the new value) and write it back under one lock."""
    with STATE_LOCK:
        value = change(state.get(key, default))
        state.set(key, value)
        return value


class StatusCache:
    """The Specivo status list, read once a day and kept in ``ctx.state`` under ``statuses``."""

    KEY = "statuses"

    def __init__(self, state: StateStore, clock: Callable[[], float] = time.time) -> None:
        self._state = state
        self._clock = clock

    def cached(self) -> list[StatusInfo]:
        raw = self._state.get(self.KEY, None)
        if not isinstance(raw, dict):
            return []
        return [StatusInfo(int(s["id"]), str(s["name"]), str(s["category"])) for s in raw.get("items", [])]

    def get(self, client: SpecivoClient) -> list[StatusInfo]:
        raw = self._state.get(self.KEY, None)
        fresh = isinstance(raw, dict) and self._clock() - float(raw.get("fetched_at", 0)) < STATUS_CACHE_SECONDS
        if fresh:
            return self.cached()
        try:
            statuses = client.statuses()
        except SpecivoError as exc:
            logger.warning("specivo: could not read the status list: %s", exc)
            return self.cached()
        self._state.set(
            self.KEY,
            {
                "fetched_at": self._clock(),
                "items": [{"id": s.id, "name": s.name, "category": s.category} for s in statuses],
            },
        )
        return statuses

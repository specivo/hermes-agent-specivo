"""What every hook needs: the context, the current settings, a Specivo client and the clock."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .config import ConfigError, Settings, load_settings
from .mcp import SpecivoClient
from .state import StateStore, StatusCache

logger = logging.getLogger(__name__)

#: What is sent in place of text that could not be redacted: never the text itself.
REDACTION_UNAVAILABLE = "Summary withheld: secret redaction is unavailable in this Hermes."


class Runtime:
    """Wraps the ``PluginContext``. Settings are read on every use, so edits apply without a restart."""

    def __init__(self, ctx: Any, clock: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        self.clock = clock
        self._reported: set[str] = set()

    @property
    def state(self) -> StateStore:
        state: StateStore = self.ctx.state
        return state

    @property
    def profile_name(self) -> str:
        return str(getattr(self.ctx, "profile_name", "") or "default")

    def settings(self) -> Settings | None:
        """The validated settings, or ``None`` after logging the problem once."""
        try:
            return load_settings(lambda key, default: self.ctx.get_config(key, default))
        except ConfigError as exc:
            self.warn_once(f"config:{exc}", "specivo: plugin settings are invalid, hooks are idle: %s", exc)
            return None

    def configured(self) -> Settings | None:
        """Settings with a URL: the plugin has been set up. ``None`` otherwise (hooks stay idle)."""
        settings = self.settings()
        if settings is None or settings.url is None:
            return None
        return settings

    def client(self, settings: Settings) -> SpecivoClient:
        return SpecivoClient(self.ctx, settings.mcp_server)

    def statuses(self) -> StatusCache:
        return StatusCache(self.state, self.clock)

    def redact(self, text: str) -> str:
        """Scrub secrets from text that leaves the machine (``ctx.redact_text`` at full strength).

        Fails closed: when Hermes offers no redactor, or it raises or answers something that is
        not text, :data:`REDACTION_UNAVAILABLE` is returned instead of *text*, and a warning is logged.
        """
        redact = getattr(self.ctx, "redact_text", None)
        if not callable(redact):
            self.warn_once("redact:missing", "specivo: Hermes offers no secret redaction; text was withheld")
            return REDACTION_UNAVAILABLE
        try:
            redacted = redact(text, force=True)
        except Exception as exc:  # any failure withholds the text; it is never sent unredacted
            logger.warning("specivo: secret redaction failed (%s); text was withheld", type(exc).__name__)
            return REDACTION_UNAVAILABLE
        if not isinstance(redacted, str):
            logger.warning("specivo: secret redaction answered no text; text was withheld")
            return REDACTION_UNAVAILABLE
        return redacted

    def warn_once(self, token: str, message: str, *args: object) -> None:
        if token in self._reported:
            return
        self._reported.add(token)
        logger.warning(message, *args)

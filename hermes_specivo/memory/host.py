"""What the memory provider takes from Hermes, with stand-ins so the plugin imports (and tests run) without it.

Hermes loads a memory provider through its own loader (``plugins/memory``), whose ``ctx`` forwards
only ``register_*`` calls: it has no ``get_config``, ``redact_text`` or ``call_mcp``. The provider
therefore reads its settings from ``config.yaml`` itself, redacts with Hermes's redactor directly
and talks to Specivo's REST API with the API key, never through the plugin context.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import ConfigGetter

#: The plugin's entry under ``plugins.entries`` (the manifest name).
PLUGIN_ID = "specivo"

# Copied from Hermes (``agent.memory_provider.TRIVIAL_PROMPT_RE``) for use outside Hermes only.
_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)[\W_]*$",
    re.IGNORECASE,
)


class _StandInMemoryProvider:
    """The default methods of Hermes's ``MemoryProvider``, used when Hermes is not importable."""

    def unavailable_reason(self) -> str:
        return ""

    def system_prompt_block(self) -> str:
        return ""

    def shutdown(self) -> None:
        return None


@dataclass(frozen=True)
class _StandInRecallStatus:
    provider_label: str
    count: int
    glyph: str = "\N{BRAIN}"


def _stand_in_is_trivial_prompt(text: str | None) -> bool:
    stripped = (text or "").strip()
    return not stripped or stripped.startswith("/") or bool(_TRIVIAL_PROMPT_RE.match(stripped))


def _stand_in_spawn_context_thread(
    target: Callable[..., Any], *, name: str, daemon: bool = True, args: tuple[Any, ...] = ()
) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=daemon, args=args)


if TYPE_CHECKING:
    MemoryProviderBase = _StandInMemoryProvider
    RecallStatus = _StandInRecallStatus
    is_trivial_prompt = _stand_in_is_trivial_prompt
    spawn_context_thread = _stand_in_spawn_context_thread
else:
    try:
        # Hermes's ``register_memory_provider`` accepts only subclasses of its own base class, and
        # background work must run under the caller's profile context (``spawn_context_thread``).
        from agent.memory_provider import MemoryProvider as MemoryProviderBase
        from agent.memory_provider import RecallStatus, is_trivial_prompt, spawn_context_thread
    except ImportError:
        MemoryProviderBase = _StandInMemoryProvider
        RecallStatus = _StandInRecallStatus
        is_trivial_prompt = _stand_in_is_trivial_prompt
        spawn_context_thread = _stand_in_spawn_context_thread


def host_config_getter() -> ConfigGetter:
    """Read ``plugins.entries.specivo.settings.<key>`` from the active profile's ``config.yaml``."""

    def get(key: str, default: Any = None) -> Any:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        node: Any = config
        for part in ("plugins", "entries", PLUGIN_ID, "settings", key):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    return get


class HermesRedactor:
    """``redact_text`` the way ``PluginContext.redact_text`` does it, for the memory loader's ``ctx``.

    Wrapped in :class:`~hermes_specivo.runtime.Runtime`, a failure here (Hermes not importable, the
    redactor raising) withholds the text instead of sending it.
    """

    def redact_text(self, text: str, *, force: bool = False) -> str:
        from agent.redact import redact_sensitive_text

        return str(
            redact_sensitive_text(text, force=force, file_read=True, secret_file=True, redact_url_credentials=True)
        )


def save_memory_setting(key: str, value: Any) -> None:
    """Write ``plugins.entries.specivo.settings.memory.<key>`` through Hermes's plugin settings writer."""
    from hermes_cli.plugins_state import save_plugin_setting

    save_plugin_setting(PLUGIN_ID, ("memory", key), value)

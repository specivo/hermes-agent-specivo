"""Plugin settings, secret lookups and the Specivo URL rule.

Settings live in Hermes's ``config.yaml`` under ``plugins.entries.specivo.settings`` and are read
through ``ctx.get_config``. Secrets are never stored here: they are environment variables declared
in ``plugin.yaml`` (``requires_env``) and resolved through Hermes's profile-aware secret scope.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

API_KEY_ENV = "SPECIVO_API_KEY"
WEBHOOK_SECRET_ENV = "SPECIVO_WEBHOOK_SECRET"  # noqa: S105 - the variable's name, not a secret
DEFAULT_MCP_SERVER = "specivo"
MIN_POLL_SECONDS = 15
DEFAULT_POLL_SECONDS = 60

#: Status names the Kanban sync moves issues to, unless the settings name others (or numeric ids).
DEFAULT_STATUSES: dict[str, str] = {
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "feedback": "Feedback",
}

_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})

#: Reads one plugin-relative setting: ``ctx.get_config(key, default)``.
ConfigGetter = Callable[[str, Any], Any]


class ConfigError(ValueError):
    """A setting is missing or invalid. The message names the key and says how to fix it."""


def is_loopback_host(host: str) -> bool:
    """Whether *host* is this machine: ``localhost`` or a loopback address."""
    name = host.strip("[]").lower()
    if name in _LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: object, key: str = "url") -> str:
    """The Specivo base URL without a trailing slash, or :class:`ConfigError`.

    ``https`` is required; plain ``http`` is accepted only for a loopback host (a local
    development server), because the API key travels in every request's headers.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            f"{key} is not set: give the address of your Specivo server, e.g. https://specivo.example.com"
        )
    text = raw.strip()
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"{key} must be an http(s) URL with a host, got {text!r}")
    if parts.username or parts.password:
        raise ConfigError(f"{key} must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ConfigError(f"{key} must not contain a query string or fragment, got {text!r}")
    if parts.scheme == "http" and not is_loopback_host(parts.hostname):
        raise ConfigError(f"{key} must use https (plain http is allowed only for localhost), got {text!r}")
    path = parts.path.rstrip("/")
    for suffix in ("/mcp", "/api/v1"):
        if path.endswith(suffix):
            raise ConfigError(f"{key} is the server's base address; drop the trailing {suffix!r} from {text!r}")
    netloc = parts.netloc.lower()
    return f"{parts.scheme}://{netloc}{path}"


def _bool(value: object, key: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {value!r}")
    return value.strip() or None


def _project_key(value: object, key: str) -> str | None:
    text = _optional_text(value, key)
    if text is None:
        return None
    if not _PROJECT_KEY_RE.fullmatch(text):
        raise ConfigError(f"{key} must be a Specivo project key such as ACME (upper case), got {text!r}")
    return text


def _mapping(value: object, key: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a mapping, got {value!r}")
    return value


def _status(value: object, key: str, default: str) -> str | int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a status name or a numeric status id, got {value!r}")
    if isinstance(value, int):
        if value < 1:
            raise ConfigError(f"{key} must be a positive status id, got {value!r}")
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        return int(text) if text.isdecimal() else text
    raise ConfigError(f"{key} must be a status name or a numeric status id, got {value!r}")


@dataclass(frozen=True)
class IssueSyncSettings:
    """``issue_sync``: Specivo issues become Hermes Kanban cards (off by default)."""

    enabled: bool = False
    project: str | None = None
    board: str | None = None
    #: Hermes profile the cards are assigned to; ``None`` means the profile the plugin runs in.
    card_assignee: str | None = None
    #: Which issues: always those assigned to the API key's account (``me``), optionally with a ``tag``.
    assignee: str = "me"
    tag: str | None = None
    poll_seconds: int = DEFAULT_POLL_SECONDS
    statuses: Mapping[str, str | int] = field(default_factory=lambda: dict(DEFAULT_STATUSES))


@dataclass(frozen=True)
class Settings:
    """Everything the plugin reads from ``plugins.entries.specivo.settings``."""

    url: str | None = None
    mcp_server: str = DEFAULT_MCP_SERVER
    default_project: str | None = None
    context_inject: bool = True
    events_enabled: bool = True
    issue_sync: IssueSyncSettings = field(default_factory=IssueSyncSettings)

    def require_url(self) -> str:
        """The base URL, or a :class:`ConfigError` saying how to set it."""
        if self.url is None:
            raise ConfigError("url is not set: run `hermes specivo setup --url https://your-specivo-host`")
        return self.url

    def sync_project(self) -> str | None:
        """The project the Kanban sync reads: ``issue_sync.project``, else ``default_project``."""
        return self.issue_sync.project or self.default_project

    def issue_link(self, issue_key: str) -> str | None:
        """The web address of an issue, when the base URL is known."""
        return f"{self.url}/issue/{issue_key}/" if self.url else None


def _issue_sync(raw: Mapping[str, Any]) -> IssueSyncSettings:
    prefix = "issue_sync"
    select = _mapping(raw.get("select"), f"{prefix}.select")
    poll = raw.get("poll_seconds", DEFAULT_POLL_SECONDS)
    if isinstance(poll, bool) or not isinstance(poll, int):
        raise ConfigError(f"{prefix}.poll_seconds must be a whole number of seconds, got {poll!r}")
    if poll < MIN_POLL_SECONDS:
        raise ConfigError(f"{prefix}.poll_seconds must be at least {MIN_POLL_SECONDS}, got {poll}")
    statuses_raw = _mapping(raw.get("statuses"), f"{prefix}.statuses")
    unknown = sorted(set(statuses_raw) - set(DEFAULT_STATUSES))
    if unknown:
        raise ConfigError(f"{prefix}.statuses has unknown keys {unknown}; use {sorted(DEFAULT_STATUSES)}")
    statuses = {
        name: _status(statuses_raw.get(name), f"{prefix}.statuses.{name}", default)
        for name, default in DEFAULT_STATUSES.items()
    }
    # Cards go to autonomous workers, so only issues assigned to the plugin's own account become
    # cards: assigning needs edit rights in the project, while anyone who can file or tag an issue
    # could otherwise put work on the board. A tag can narrow the selection further.
    assignee = select.get("assignee", "me")
    if assignee != "me":
        raise ConfigError(
            f"{prefix}.select.assignee must be 'me' (the API key's account); got {assignee!r}. "
            "Use select.tag to narrow the selection"
        )
    tag = _optional_text(select.get("tag"), f"{prefix}.select.tag")
    return IssueSyncSettings(
        enabled=_bool(raw.get("enabled"), f"{prefix}.enabled", False),
        project=_project_key(raw.get("project"), f"{prefix}.project"),
        board=_optional_text(raw.get("board"), f"{prefix}.board"),
        card_assignee=_optional_text(raw.get("card_assignee"), f"{prefix}.card_assignee"),
        assignee=assignee,
        tag=tag,
        poll_seconds=poll,
        statuses=statuses,
    )


def load_settings(get: ConfigGetter) -> Settings:
    """Read and validate the plugin's settings; :class:`ConfigError` names the first bad key.

    A missing ``url`` is not an error here (the plugin loads before ``setup`` runs); callers that
    need it use :meth:`Settings.require_url`.
    """
    raw_url = get("url", None)
    url = normalize_base_url(raw_url) if raw_url not in (None, "") else None
    server = get("mcp_server", None) or DEFAULT_MCP_SERVER
    if not isinstance(server, str) or not _SERVER_NAME_RE.fullmatch(server):
        raise ConfigError(f"mcp_server must be a simple name such as 'specivo', got {server!r}")
    context = _mapping(get("context", None), "context")
    events = _mapping(get("events", None), "events")
    sync = _mapping(get("issue_sync", None), "issue_sync")
    settings = Settings(
        url=url,
        mcp_server=server,
        default_project=_project_key(get("default_project", None), "default_project"),
        context_inject=_bool(context.get("inject"), "context.inject", True),
        events_enabled=_bool(events.get("enabled"), "events.enabled", True),
        issue_sync=_issue_sync(sync),
    )
    if settings.issue_sync.enabled and settings.sync_project() is None:
        raise ConfigError("issue_sync.enabled is true but no project is set: set issue_sync.project or default_project")
    return settings


def get_secret(name: str) -> str | None:
    """Resolve a secret by its environment variable name through Hermes's profile secret scope.

    Outside Hermes (tests, tooling) it falls back to the process environment. The value is never
    logged or printed by this plugin.
    """
    try:
        from agent.secret_scope import get_secret as hermes_get_secret
    except ImportError:
        value = os.environ.get(name)
    else:
        value = hermes_get_secret(name, None)
    return value if value else None

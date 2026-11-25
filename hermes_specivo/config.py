"""Plugin settings, secret lookups and the Specivo URL rule.

Settings live in Hermes's ``config.yaml`` under ``plugins.entries.specivo.settings`` and are read
through ``ctx.get_config``. Secrets are never stored here: they are environment variables declared
in ``plugin.yaml`` (``requires_env``) and resolved through Hermes's profile-aware secret scope.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

API_KEY_ENV = "SPECIVO_API_KEY"
WEBHOOK_SECRET_ENV = "SPECIVO_WEBHOOK_SECRET"  # noqa: S105 - the variable's name, not a secret
DEFAULT_MCP_SERVER = "specivo"
MIN_POLL_SECONDS = 15
#: Each poll is one ``specivo_list_issues`` call (and one Specivo audit log row): every five minutes
#: keeps that to 288 a day; a newly assigned issue becomes a card within this time.
DEFAULT_POLL_SECONDS = 300

#: The ai-usage reporter run when a session ends (``usage_report``): it sends Hermes's usage.
DEFAULT_USAGE_COMMAND: tuple[str, ...] = ("ai-usage", "push", "--source", "hermes")
#: At most one run per this many seconds: a gateway ends a session after every exchange.
DEFAULT_USAGE_INTERVAL = 300
MIN_USAGE_INTERVAL = 60
MAX_USAGE_INTERVAL = 86400
#: A run that takes longer is stopped (the next one sends what it left).
USAGE_TIMEOUT_SECONDS = 600
USAGE_REPORT_KEYS = frozenset({"enabled", "command", "min_interval_seconds"})

#: Status names the Kanban sync moves issues to, unless the settings name others (or numeric ids).
DEFAULT_STATUSES: dict[str, str] = {
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "feedback": "Feedback",
}

#: The memory provider's recall budget: Hermes injects the recalled text before every turn.
DEFAULT_MEMORY_MAX_HITS = 5
MAX_MEMORY_HITS = 10
DEFAULT_MEMORY_MAX_CHARS = 1800
MIN_MEMORY_MAX_CHARS = 200
MAX_MEMORY_MAX_CHARS = 8000
DEFAULT_NOTES_PAGE = "Agent Notes"
MEMORY_KEYS = frozenset(
    {"project", "cwd_projects", "chat_projects", "notes_page", "max_hits", "max_chars", "mirror_builtin"}
)

_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
#: A Hermes Kanban board slug, as ``kanban_db._normalize_board_slug`` accepts it (after lower-casing).
BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
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


def normalize_board(value: object) -> str | None:
    """A board slug normalised the way Hermes does (strip, lower case), or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    slug = value.strip().lower()
    return slug if BOARD_SLUG_RE.fullmatch(slug) else None


def _board(value: object, key: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    slug = normalize_board(value)
    if slug is None:
        raise ConfigError(
            f"{key} must be a Hermes board slug (1-64 lower-case letters, digits, '-' or '_', "
            f"not starting with '-' or '_'), got {value!r}"
        )
    return slug


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
class MemorySettings:
    """``memory``: the optional memory provider (active only with ``memory.provider: specivo``)."""

    #: Project recall and notes use; else ``cwd_projects``, ``chat_projects``, then ``default_project``.
    project: str | None = None
    #: Working directory prefix -> project key; the longest matching prefix wins.
    cwd_projects: Mapping[str, str] = field(default_factory=dict)
    #: ``<platform>:<chat_id>`` -> project key, for gateway chats.
    chat_projects: Mapping[str, str] = field(default_factory=dict)
    #: Title of the wiki page ``specivo_remember`` appends to, one per project.
    notes_page: str = DEFAULT_NOTES_PAGE
    max_hits: int = DEFAULT_MEMORY_MAX_HITS
    max_chars: int = DEFAULT_MEMORY_MAX_CHARS
    #: Mirror Hermes's built-in MEMORY.md and USER.md writes to the notes page (off by default).
    mirror_builtin: bool = False


@dataclass(frozen=True)
class UsageReportSettings:
    """``usage_report``: run the ai-usage reporter when a session ends (off by default)."""

    enabled: bool = False
    #: The reporter's argv; it gets ``SPECIVO_API_KEY`` from the plugin's secret scope.
    command: tuple[str, ...] = DEFAULT_USAGE_COMMAND
    min_interval_seconds: int = DEFAULT_USAGE_INTERVAL


@dataclass(frozen=True)
class Settings:
    """Everything the plugin reads from ``plugins.entries.specivo.settings``."""

    url: str | None = None
    mcp_server: str = DEFAULT_MCP_SERVER
    default_project: str | None = None
    context_inject: bool = True
    events_enabled: bool = True
    issue_sync: IssueSyncSettings = field(default_factory=IssueSyncSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    usage_report: UsageReportSettings = field(default_factory=UsageReportSettings)

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
        board=_board(raw.get("board"), f"{prefix}.board"),
        card_assignee=_optional_text(raw.get("card_assignee"), f"{prefix}.card_assignee"),
        assignee=assignee,
        tag=tag,
        poll_seconds=poll,
        statuses=statuses,
    )


def _bounded_int(value: object, key: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _project_map(value: object, key: str) -> dict[str, str]:
    """A mapping of non-empty strings to project keys."""
    result: dict[str, str] = {}
    for name, project in _mapping(value, key).items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{key} keys must be non-empty strings, got {name!r}")
        checked = _project_key(project, f"{key}.{name}")
        if checked is None:
            raise ConfigError(f"{key}.{name} must name a project key such as ACME")
        result[name.strip()] = checked
    return result


def _memory(raw: Mapping[str, Any]) -> MemorySettings:
    prefix = "memory"
    unknown = sorted(str(k) for k in set(raw) - MEMORY_KEYS)
    if unknown:
        raise ConfigError(f"{prefix} has unknown keys {unknown}; use {sorted(MEMORY_KEYS)}")
    notes_page = _optional_text(raw.get("notes_page"), f"{prefix}.notes_page") or DEFAULT_NOTES_PAGE
    if len(notes_page) > 255 or not any(ch.isalnum() for ch in notes_page):
        raise ConfigError(f"{prefix}.notes_page must be a wiki page title of up to 255 characters, got {notes_page!r}")
    return MemorySettings(
        project=_project_key(raw.get("project"), f"{prefix}.project"),
        cwd_projects=_project_map(raw.get("cwd_projects"), f"{prefix}.cwd_projects"),
        chat_projects=_project_map(raw.get("chat_projects"), f"{prefix}.chat_projects"),
        notes_page=notes_page,
        max_hits=_bounded_int(raw.get("max_hits"), f"{prefix}.max_hits", DEFAULT_MEMORY_MAX_HITS, 1, MAX_MEMORY_HITS),
        max_chars=_bounded_int(
            raw.get("max_chars"),
            f"{prefix}.max_chars",
            DEFAULT_MEMORY_MAX_CHARS,
            MIN_MEMORY_MAX_CHARS,
            MAX_MEMORY_MAX_CHARS,
        ),
        mirror_builtin=_bool(raw.get("mirror_builtin"), f"{prefix}.mirror_builtin", False),
    )


def _usage_report(raw: Mapping[str, Any]) -> UsageReportSettings:
    prefix = "usage_report"
    unknown = sorted(str(k) for k in set(raw) - USAGE_REPORT_KEYS)
    if unknown:
        raise ConfigError(f"{prefix} has unknown keys {unknown}; use {sorted(USAGE_REPORT_KEYS)}")
    command_raw = raw.get("command")
    if command_raw is None:
        command = DEFAULT_USAGE_COMMAND
    elif isinstance(command_raw, str):
        try:
            command = tuple(shlex.split(command_raw))
        except ValueError as exc:
            raise ConfigError(f"{prefix}.command cannot be read: {exc}") from None
    elif isinstance(command_raw, list) and all(isinstance(part, str) for part in command_raw):
        command = tuple(command_raw)
    else:
        raise ConfigError(f"{prefix}.command must be a command line or a list of strings, got {command_raw!r}")
    if not command or not command[0].strip():
        raise ConfigError(f"{prefix}.command must name the reporter, e.g. 'ai-usage push --source hermes'")
    return UsageReportSettings(
        enabled=_bool(raw.get("enabled"), f"{prefix}.enabled", False),
        command=command,
        min_interval_seconds=_bounded_int(
            raw.get("min_interval_seconds"),
            f"{prefix}.min_interval_seconds",
            DEFAULT_USAGE_INTERVAL,
            MIN_USAGE_INTERVAL,
            MAX_USAGE_INTERVAL,
        ),
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
    memory = _mapping(get("memory", None), "memory")
    usage = _mapping(get("usage_report", None), "usage_report")
    settings = Settings(
        url=url,
        mcp_server=server,
        default_project=_project_key(get("default_project", None), "default_project"),
        context_inject=_bool(context.get("inject"), "context.inject", True),
        events_enabled=_bool(events.get("enabled"), "events.enabled", True),
        issue_sync=_issue_sync(sync),
        memory=_memory(memory),
        usage_report=_usage_report(usage),
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

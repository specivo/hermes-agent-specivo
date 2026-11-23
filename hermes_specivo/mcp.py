"""Calls to the Specivo MCP server through ``ctx.call_mcp``, and parsers for its text answers.

The plugin never opens its own connection: Hermes's MCP client (the one the agent uses, with
the ``Authorization`` header from ``config.yaml``) carries every call, so the plugin never
holds the API key. ``ctx.call_mcp`` needs ``plugins.entries.specivo.mcp_allowlist: [specivo]``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]*-\d+)\b")
_ISSUE_LINE_RE = re.compile(r"^\s+([A-Z][A-Z0-9_]*-\d+)\s+\[([^\]]+)\]\s+(.*?)(?:\s+\[tags: [^\]]*\])?\s*$")
_LIST_TOTAL_RE = re.compile(r"\((\d+) total")
_STATUS_LINE_RE = re.compile(r"^\s+(\d+)\s+(.+?)\s+\[(\w+)\]\s*$")


class McpCaller(Protocol):
    """The slice of ``PluginContext`` this module needs."""

    def call_mcp(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]: ...


class SpecivoError(RuntimeError):
    """A Specivo tool call failed: not allowed, unreachable, or refused by the server."""


def result_text(envelope: object) -> str:
    """The text of a ``ctx.call_mcp`` envelope, or :class:`SpecivoError`.

    Specivo tools answer one string, which Hermes hands back as ``{"ok": true, "result": ...}``;
    the result may itself be the server's ``{"result": "..."}`` wrapper or a list of content blocks.
    """
    if not isinstance(envelope, dict):
        raise SpecivoError(f"unexpected MCP answer: {envelope!r}")
    if not envelope.get("ok", False):
        raise SpecivoError(str(envelope.get("error") or "the MCP call failed"))
    return _flatten(envelope.get("result"))


def _flatten(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                return value
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                if "error" in parsed and "result" not in parsed:
                    raise SpecivoError(str(parsed["error"]))
                return _flatten(parsed.get("result"))
        return value
    if isinstance(value, dict):
        if "error" in value and "result" not in value:
            raise SpecivoError(str(value["error"]))
        if "result" in value:
            return _flatten(value["result"])
        if "text" in value:
            return _flatten(value["text"])
        return json.dumps(value)
    if isinstance(value, list):
        return "\n".join(_flatten(item) for item in value)
    return str(value)


@dataclass(frozen=True)
class IssueRow:
    """One line of ``specivo_list_issues``."""

    key: str
    status: str
    subject: str


@dataclass(frozen=True)
class IssuePage:
    rows: list[IssueRow]
    total: int


@dataclass(frozen=True)
class StatusInfo:
    """One status of ``specivo_list_lookups``: id, name and category (backlog, active, done, closed)."""

    id: int
    name: str
    category: str

    @property
    def is_finished(self) -> bool:
        return self.category in ("done", "closed")


def parse_issue_list(text: str) -> IssuePage:
    """Parse ``specivo_list_issues`` output: a ``(N total, ...)`` header and ``KEY  [Status]  Subject`` lines."""
    match = _LIST_TOTAL_RE.search(text.splitlines()[0] if text else "")
    rows = []
    for line in text.splitlines()[1:]:
        found = _ISSUE_LINE_RE.match(line)
        if found:
            rows.append(IssueRow(key=found.group(1), status=found.group(2), subject=found.group(3)))
    return IssuePage(rows=rows, total=int(match.group(1)) if match else len(rows))


def parse_statuses(text: str) -> list[StatusInfo]:
    """The ``Statuses:`` section of ``specivo_list_lookups``."""
    statuses: list[StatusInfo] = []
    in_section = False
    for line in text.splitlines():
        if not line.startswith(" "):
            in_section = line.strip() == "Statuses:"
            continue
        if in_section:
            found = _STATUS_LINE_RE.match(line)
            if found:
                statuses.append(StatusInfo(int(found.group(1)), found.group(2), found.group(3)))
    return statuses


def first_issue_key(text: str) -> str | None:
    match = ISSUE_KEY_RE.search(text or "")
    return match.group(1) if match else None


class SpecivoClient:
    """Typed wrappers over the Specivo tools the plugin uses. Every call may raise :class:`SpecivoError`."""

    def __init__(self, ctx: McpCaller, server: str, timeout: float = 15) -> None:
        self._ctx = ctx
        self.server = server
        self._timeout = timeout

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        args = {k: v for k, v in arguments.items() if v is not None}
        try:
            envelope = self._ctx.call_mcp(self.server, tool, args, timeout=self._timeout)
        except PermissionError as exc:
            raise SpecivoError(
                f"{exc} Run `hermes specivo setup` to add {self.server!r} to the plugin's mcp_allowlist."
            ) from exc
        except SpecivoError:
            raise
        except Exception as exc:  # the MCP client raises many kinds; the caller only needs "it failed"
            raise SpecivoError(f"{tool} failed: {exc}") from exc
        return result_text(envelope)

    def whoami(self) -> str:
        return self.call("specivo_whoami", {})

    def agent_session_set(
        self,
        external_session_id: str | None = None,
        parent_session_id: str | None = None,
        issue_ref: str | None = None,
    ) -> str:
        return self.call(
            "specivo_agent_session",
            {
                "op": "set",
                "external_session_id": external_session_id,
                "parent_session_id": parent_session_id,
                "issue_ref": issue_ref,
            },
        )

    def agent_session_get(self) -> str:
        return self.call("specivo_agent_session", {"op": "get"})

    def list_issues(
        self,
        project_key: str,
        *,
        assignee: str | None = None,
        tag: str | None = None,
        status: str = "open",
        offset: int = 0,
        limit: int = 100,
    ) -> IssuePage:
        text = self.call(
            "specivo_list_issues",
            {
                "project_key": project_key,
                "status": status,
                "assignee": assignee,
                "tag": tag,
                "offset": offset,
                "limit": limit,
                "sort": "created_at:asc",
            },
        )
        return parse_issue_list(text)

    def iter_open_issues(
        self, project_key: str, *, assignee: str | None, tag: str | None, max_issues: int = 500
    ) -> tuple[list[IssueRow], bool]:
        """Every open issue matching the filters, up to *max_issues*; the flag says whether all were read."""
        rows: list[IssueRow] = []
        offset = 0
        while True:
            page = self.list_issues(project_key, assignee=assignee, tag=tag, offset=offset)
            rows.extend(page.rows)
            offset += len(page.rows)
            if not page.rows or offset >= page.total:
                return rows, True
            if offset >= max_issues:
                return rows, False

    def statuses(self) -> list[StatusInfo]:
        return parse_statuses(self.call("specivo_list_lookups", {}))

    def update_issue(self, issue_ref: str, *, status_id: int | None = None, notes: str | None = None) -> str:
        return self.call("specivo_update_issue", {"issue_ref": issue_ref, "status_id": status_id, "notes": notes})


def resolve_status_id(wanted: str | int, statuses: Iterable[StatusInfo]) -> int | None:
    """A configured status (numeric id or case-insensitive name) as an id, or ``None`` when unknown."""
    if isinstance(wanted, int):
        return wanted
    for status in statuses:
        if status.name.casefold() == wanted.casefold():
            return status.id
    return None

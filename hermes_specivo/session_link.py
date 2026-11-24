"""The current issue: links the Hermes session to Specivo's agent session and keeps the issue it works on.

- ``pre_llm_call``: every turn calls ``specivo_agent_session(op="set", external_session_id=...)``
  with Hermes's ``session_id``. Specivo keeps one agent session per external id on the MCP
  connection and makes it the connection's current one, so subagents and gateway chats that share
  the connection each get their own, and usage and webhook events join by id. The parent session
  id and the cached issue go along only until the session is linked. When the current issue is
  known, the turn gets one context line.
- ``post_tool_call``: when the agent starts work on an issue through Specivo's own tools (status or
  assignee change, timer start, a new issue, an explicit ``specivo_agent_session`` set), the key is
  cached for that session; an explicit set with an empty issue clears it. Specivo records the
  working issue itself; the cache feeds the prompt.
- ``/specivo issue KEY`` sets it by hand, ``/specivo issue clear`` clears it on both sides,
  ``/specivo status`` shows it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import Settings
from .mcp import ISSUE_KEY_RE, SpecivoError, first_issue_key
from .runtime import Runtime
from .state import update_state

logger = logging.getLogger(__name__)

SESSIONS_KEY = "sessions"
MAX_SESSIONS = 200
#: After a failed link or select, turns skip the call for this long, so an unreachable server does not slow
#: every turn.
LINK_RETRY_SECONDS = 300
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_EXACT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

HELP = (
    "Usage: /specivo issue KEY   set the issue this session works on (e.g. ACME-12)\n"
    "       /specivo issue clear forget it for this session\n"
    "       /specivo status      show the linked Specivo agent session and issue"
)


def valid_session_id(value: object) -> str | None:
    """The id when Specivo accepts it as an external session id, else ``None``."""
    return value if isinstance(value, str) and _SESSION_ID_RE.fullmatch(value) else None


def mcp_tool_prefix(server: str) -> str:
    """``mcp__<server>__``: how Hermes names a remote MCP tool (see ``tools/mcp_tool_schema.py``)."""
    return f"mcp__{re.sub(r'[^A-Za-z0-9_]', '_', server)}__"


class SessionLink:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._retry_after = 0.0

    # -- state -------------------------------------------------------------------------------

    def _entry(self, session_id: str) -> dict[str, Any]:
        sessions = self.rt.state.get(SESSIONS_KEY, {}) or {}
        entry = sessions.get(session_id)
        return dict(entry) if isinstance(entry, dict) else {}

    def _save(self, session_id: str, **fields: Any) -> None:
        def change(stored: Any) -> dict[str, Any]:
            sessions: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
            entry = dict(sessions.get(session_id) or {})
            entry.update(fields)
            entry["seen"] = self.rt.clock()
            sessions[session_id] = entry
            return sessions

        update_state(self.rt.state, SESSIONS_KEY, {}, change)

    def current_issue(self, session_id: str) -> tuple[str | None, str | None]:
        entry = self._entry(session_id)
        return entry.get("issue"), entry.get("status")

    # -- hooks -------------------------------------------------------------------------------

    def on_pre_llm_call(
        self, session_id: str = "", parent_session_id: str = "", is_first_turn: bool = False, **_: Any
    ) -> dict[str, str] | None:
        settings = self.rt.configured()
        sid = valid_session_id(session_id)
        if settings is None or sid is None:
            return None
        entry = self._entry(sid)
        if self.rt.clock() >= self._retry_after:
            self._select(settings, sid, entry, parent_session_id)
        if not settings.context_inject:
            return None
        issue, status = entry.get("issue"), entry.get("status")
        if not issue:
            return None
        suffix = f" ({status})" if status else ""
        return {"context": f"Current Specivo issue: {issue}{suffix}"}

    def _select(self, settings: Settings, sid: str, entry: dict[str, Any], parent_session_id: str) -> None:
        """Make *sid* the connection's current agent session; on its first turn also link parent and issue.

        The call is made on every turn, never skipped from a cache of the last selected id: other
        sessions on the connection (subagents, gateway chats, the agent's own
        ``specivo_agent_session`` calls) and an MCP reconnect change the server's selection without
        this plugin seeing it. Re-selecting the current session is cheap and writes no audit entry.
        """
        linked = bool(entry.get("linked"))
        parent = None if linked else valid_session_id(parent_session_id)
        issue = None if linked else entry.get("issue")
        try:
            client = self.rt.client(settings)
            client.agent_session_set(external_session_id=sid, parent_session_id=parent, issue_ref=issue)
        except SpecivoError as exc:
            self._retry_after = self.rt.clock() + LINK_RETRY_SECONDS
            self.rt.warn_once(f"link:{sid}", "specivo: could not link session %s: %s", sid, exc)
            return
        if not linked:
            # The first turn, or a resumed session this profile has not linked yet.
            self._save(sid, linked=True, parent=parent)

    def _clear_on_specivo(self, settings: Settings, sid: str) -> SpecivoError | None:
        """Clear the working issue of *sid*'s agent session; the failure, if any, is returned."""
        try:
            self.rt.client(settings).agent_session_set(external_session_id=sid, issue_ref="")
        except SpecivoError as exc:
            return exc
        return None

    def on_post_tool_call(
        self,
        tool_name: str = "",
        args: dict[str, Any] | None = None,
        result: Any = None,
        session_id: str | None = None,
        status: str | None = None,
        **_: Any,
    ) -> None:
        settings = self.rt.configured()
        sid = valid_session_id(session_id)
        if settings is None or sid is None or status == "error":
            return
        prefix = mcp_tool_prefix(settings.mcp_server)
        if not tool_name.startswith(prefix):
            return
        tool = tool_name[len(prefix) :]
        args = args or {}
        issue_ref = args.get("issue_ref") if isinstance(args.get("issue_ref"), str) else None
        if tool == "specivo_update_issue" and issue_ref:
            if "status_id" not in args and "assigned_to_id" not in args:
                return
            self._from_status_change(sid, issue_ref, args.get("status_id"))
        elif tool == "specivo_timer" and args.get("op") == "start" and issue_ref:
            self._set_issue(sid, issue_ref, None)
        elif tool == "specivo_agent_session" and args.get("op") == "set" and issue_ref is not None:
            if issue_ref.strip():
                self._set_issue(sid, issue_ref, None)
            else:  # Specivo clears the working issue on an empty (or blank) issue_ref
                self._save(sid, issue=None, status=None)
        elif tool == "specivo_create_issue":
            key = first_issue_key(result if isinstance(result, str) else str(result or ""))
            if key:
                self._set_issue(sid, key, None)

    def _from_status_change(self, sid: str, issue_ref: str, status_id: object) -> None:
        status_name: str | None = None
        if isinstance(status_id, int) and not isinstance(status_id, bool):
            settings = self.rt.configured()
            statuses = self.rt.statuses().get(self.rt.client(settings)) if settings else []
            info = next((s for s in statuses if s.id == status_id), None)
            if info is not None and info.is_finished:
                current, _ = self.current_issue(sid)
                if current == issue_ref:
                    self._save(sid, issue=None, status=None)
                    failure = self._clear_on_specivo(settings, sid) if settings else None
                    if failure is not None:
                        logger.warning("specivo: could not clear the working issue of session %s: %s", sid, failure)
                return
            status_name = info.name if info else None
        self._set_issue(sid, issue_ref, status_name)

    def _set_issue(self, sid: str, issue_ref: str, status: str | None) -> None:
        if not _EXACT_KEY_RE.fullmatch(issue_ref):
            return
        self._save(sid, issue=issue_ref, status=status)

    def on_session_end(self, **_: Any) -> None:
        """Keep the per-session cache bounded: drop the least recently seen beyond ``MAX_SESSIONS``."""

        def prune(stored: Any) -> dict[str, Any]:
            sessions: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
            if len(sessions) <= MAX_SESSIONS:
                return sessions
            ordered = sorted(sessions.items(), key=lambda item: float(item[1].get("seen", 0)), reverse=True)
            return dict(ordered[:MAX_SESSIONS])

        update_state(self.rt.state, SESSIONS_KEY, {}, prune)

    # -- /specivo ----------------------------------------------------------------------------

    def command(self, raw_args: str = "", session_id: str | None = None, **_: Any) -> str:
        words = (raw_args or "").split()
        settings = self.rt.settings()
        if settings is None:
            return "The specivo plugin settings are invalid; run `hermes specivo doctor` for details."
        if settings.url is None:
            return "Specivo is not set up yet: run `hermes specivo setup --url https://your-specivo-host`."
        sid = valid_session_id(session_id)
        if not words or words[0] == "help":
            return HELP
        if words[0] == "status":
            return self._status(sid)
        if words[0] == "issue" and len(words) == 2:
            if sid is None:
                return "No session is active here, so there is nothing to attach the issue to."
            if words[1] == "clear":
                failure = self._clear_on_specivo(settings, sid)
                self._save(sid, issue=None, status=None)
                if failure is not None:
                    return f"Current Specivo issue cleared here; Specivo could not be updated: {failure}"
                return "Current Specivo issue cleared for this session, here and on Specivo."
            key = words[1].upper()
            if not ISSUE_KEY_RE.fullmatch(key):
                return f"{words[1]!r} is not an issue key such as ACME-12."
            try:
                self.rt.client(settings).agent_session_set(external_session_id=sid, issue_ref=key)
            except SpecivoError as exc:
                return f"Specivo refused {key}: {exc}"
            self._save(sid, issue=key, status=None, linked=True)
            return f"Current Specivo issue: {key}"
        return HELP

    def _status(self, sid: str | None) -> str:
        settings = self.rt.configured()
        lines: list[str] = []
        if sid:
            issue, status = self.current_issue(sid)
            lines.append(f"Hermes session: {sid}")
            lines.append(f"Current issue (this plugin): {issue or '(none)'}{f' ({status})' if status else ''}")
        if settings is not None:
            try:
                lines.append("Specivo agent session:")
                shown = self.rt.client(settings).agent_session_get(external_session_id=sid)
                lines.extend(f"  {line}" for line in shown.splitlines())
            except SpecivoError as exc:
                lines.append(f"  unavailable: {exc}")
        return "\n".join(lines)

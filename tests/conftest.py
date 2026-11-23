"""A fake ``PluginContext``, a stub Specivo MCP server and a fake Kanban board. Nothing touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hermes_specivo.runtime import Runtime

BASE_URL = "https://specivo.example.test"

LOOKUPS = """Trackers:

  1  Bug
  2  Feature

Statuses:
  1  New  [backlog]
  2  In Progress  [active]
  3  Resolved  [done]
  4  Feedback  [active]
  5  Closed  [closed]

Priorities:
  2  Normal  [default]

Time entry activities:
  9  Development  [default]"""

WHOAMI = "user_id: 7\nlogin: agent-bot\ndisplay_name: Agent Bot\nemail: agent@example.test\nis_admin: False"


def issue_list(project: str, rows: list[tuple[str, str, str]], total: int | None = None) -> str:
    lines = [f"Issues for {project} ({len(rows) if total is None else total} total, filter=open):", ""]
    lines += [f"  {key}  [{status}]  {subject}" for key, status, subject in rows] or ["  (none)"]
    return "\n".join(lines)


class FakeState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.writes = 0

    def get(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, default)
        return json.loads(json.dumps(value)) if value is not None else value

    def set(self, key: str, value: Any) -> None:
        self.data[key] = json.loads(json.dumps(value))  # JSON only, like Hermes
        self.writes += 1


Handler = Callable[[dict[str, Any]], str]


class StubMcp:
    """Answers ``ctx.call_mcp`` like Hermes does: ``{"ok": true, "result": <tool text>}``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.handlers: dict[str, Handler | Exception] = {
            "specivo_whoami": lambda a: WHOAMI,
            "specivo_list_lookups": lambda a: LOOKUPS,
            "specivo_agent_session": lambda a: "Agent session updated.\nid: 1",
            "specivo_update_issue": lambda a: f"Updated issue {a['issue_ref']}",
            "specivo_list_issues": lambda a: issue_list(a["project_key"], []),
        }
        self.allowlist: set[str] = {"specivo"}

    def __call__(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        if server not in self.allowlist:
            raise PermissionError(f"Plugin 'specivo' is not allowed to call MCP server {server!r}.")
        args = dict(arguments or {})
        self.calls.append((server, tool, args))
        handler = self.handlers.get(tool)
        if handler is None:
            return {"ok": False, "error": f"unknown tool {tool}"}
        if isinstance(handler, Exception):
            return {"ok": False, "error": str(handler)}
        return {"ok": True, "result": handler(args)}

    def tool_calls(self, tool: str) -> list[dict[str, Any]]:
        return [args for _, name, args in self.calls if name == tool]


class FakeBoard:
    """The ``kanban_*`` tools through ``ctx.dispatch_tool``."""

    def __init__(self) -> None:
        #: One task table per board, as in Hermes (each board has its own kanban.db): ids repeat across boards.
        self.boards: dict[str, dict[str, dict[str, Any]]] = {"default": {}}
        self.tasks = self.boards["default"]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_complete: Callable[[str, str | None], None] | None = None
        self.fail: set[str] = set()

    def __call__(self, tool: str, args: dict[str, Any], **_: Any) -> str:
        self.calls.append((tool, dict(args)))
        if tool in self.fail:
            return json.dumps({"error": f"{tool} is unavailable"})
        tasks = self.boards.setdefault(args.get("board") or "default", {})
        if tool == "kanban_create":
            key = args.get("idempotency_key")
            for tid, task in tasks.items():
                if key and task.get("idempotency_key") == key:
                    return json.dumps({"ok": True, "task_id": tid})
            tid = f"t_{len(tasks) + 1}"
            tasks[tid] = {"status": "ready", "comments": [], **args}
            return json.dumps({"ok": True, "task_id": tid})
        if tool == "kanban_comment":  # append-only, like Hermes
            target = tasks.get(args["task_id"])
            if target is None:
                return json.dumps({"error": f"unknown task {args['task_id']}"})
            target.setdefault("comments", []).append({"author": "worker", "body": args["body"]})
            return json.dumps({"ok": True, "task_id": args["task_id"], "comment_id": len(target["comments"])})
        if tool == "kanban_complete":
            tasks[args["task_id"]]["status"] = "done"
            if self.on_complete:
                self.on_complete(args["task_id"], args.get("summary"))
            return json.dumps({"ok": True, "task_id": args["task_id"]})
        if tool == "kanban_show":
            shown = tasks.get(args["task_id"])
            if shown is None:
                return json.dumps({"error": "no such task"})
            return json.dumps(
                {
                    "task": {
                        "id": args["task_id"],
                        "title": shown["title"],
                        "body": shown.get("body"),
                        "assignee": shown.get("assignee"),
                    },
                    "comments": list(shown.get("comments") or []),
                }
            )
        return json.dumps({"error": f"unexpected tool {tool}"})

    def created(self) -> list[dict[str, Any]]:
        return [args for tool, args in self.calls if tool == "kanban_create"]


class FakeCtx:
    """Records registrations and serves ``plugins.entries.specivo.settings`` from a dict."""

    plugin_id = "specivo"
    profile_name = "worker"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings: dict[str, Any] = dict(settings or {})
        self.state = FakeState()
        self.mcp = StubMcp()
        self.board = FakeBoard()
        self.hooks: dict[str, list[Callable[..., Any]]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}
        self.prompt_sections: dict[str, Any] = {}
        self.skills: list[tuple[str, Path]] = []
        self.redacted: list[str] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def call_mcp(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        return self.mcp(server, tool, arguments, timeout)

    def dispatch_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        return self.board(tool_name, args, **kwargs)

    def redact_text(self, text: str, *, force: bool = False) -> str:
        self.redacted.append(text)
        return text.replace("sk-test-secret", "«redacted»")

    def register_hook(self, name: str, callback: Callable[..., Any], **_: Any) -> None:
        self.hooks.setdefault(name, []).append(callback)

    def register_command(self, name: str, handler: Callable[..., Any], **kwargs: Any) -> None:
        self.commands[name] = {"handler": handler, **kwargs}

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands[kwargs["name"]] = kwargs

    def register_system_prompt_section(self, section_id: str, content: Any, **kwargs: Any) -> None:
        self.prompt_sections[section_id] = content

    def register_skill(self, name: str, path: Path) -> None:
        self.skills.append((name, path))


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ctx() -> FakeCtx:
    return FakeCtx({"url": BASE_URL, "default_project": "ACME"})


@pytest.fixture
def runtime(ctx: FakeCtx, clock: Clock) -> Runtime:
    return Runtime(ctx, clock=clock)


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests decide which secrets exist; the developer's environment never leaks in."""
    monkeypatch.delenv("SPECIVO_API_KEY", raising=False)
    monkeypatch.delenv("SPECIVO_WEBHOOK_SECRET", raising=False)

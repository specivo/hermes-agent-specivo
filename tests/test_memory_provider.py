"""The memory provider against a stub Specivo on 127.0.0.1: recall, remember, mirror and setup."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import FakeCtx
from specivo_stub import StubSpecivo, running_stub

from hermes_specivo.config import ConfigError
from hermes_specivo.memory import provider as provider_module
from hermes_specivo.memory.provider import SpecivoMemoryProvider
from hermes_specivo.runtime import REDACTION_UNAVAILABLE, Runtime

KEY = "spv_test_key"
NOW = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
CANARY = "sk-test-secret"

ISSUE = {
    "result_type": "issue",
    "title": "ACME-12",
    "subtitle": "Backup check fails",
    "snippet": "the <mark>backup</mark>",
}
WIKI = {
    "result_type": "wiki",
    "title": "Deployment checklist",
    "subtitle": "deployment-checklist",
    "snippet": "run the checks",
    "project_key": "ACME",
}


@pytest.fixture
def stub() -> Iterator[StubSpecivo]:
    with running_stub() as server:
        yield server


def make_provider(
    stub: StubSpecivo,
    memory: dict[str, Any] | None = None,
    *,
    key: str | None = KEY,
    redactor: Any = None,
    **kwargs: Any,
) -> SpecivoMemoryProvider:
    raw: dict[str, Any] = {"url": stub.url, "memory": {"project": "ACME", **(memory or {})}}
    return SpecivoMemoryProvider(
        config=lambda name, default: raw.get(name, default),
        redactor=redactor or Runtime(FakeCtx()).redact,
        secret=lambda name: key if name == "SPECIVO_API_KEY" else None,
        now=lambda: NOW,
        **kwargs,
    )


def ready(stub: StubSpecivo, memory: dict[str, Any] | None = None, **kwargs: Any) -> SpecivoMemoryProvider:
    agent_context = kwargs.pop("ctx", "primary")
    provider = make_provider(stub, memory, **kwargs)
    provider.initialize("s1", hermes_home="/tmp/hermes-home", platform="cli", agent_context=agent_context)
    return provider


def call(provider: SpecivoMemoryProvider, tool: str, **args: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(provider.handle_tool_call(tool, args))
    return result


# -- availability and opt-in ----------------------------------------------------------------------


def test_available_only_with_url_and_key_and_without_network(stub: StubSpecivo) -> None:
    assert make_provider(stub).is_available()
    assert not make_provider(stub, key=None).is_available()
    assert "SPECIVO_API_KEY" in make_provider(stub, key=None).unavailable_reason()
    no_url = SpecivoMemoryProvider(config=lambda name, default: default, secret=lambda name: KEY)
    assert not no_url.is_available() and "url" in no_url.unavailable_reason()
    bad = SpecivoMemoryProvider(config=lambda name, default: {"max_hits": 0} if name == "memory" else default)
    assert not bad.is_available() and "invalid" in bad.unavailable_reason()
    assert stub.requests == []


def test_uninitialised_or_unconfigured_provider_does_nothing(stub: StubSpecivo) -> None:
    provider = make_provider(stub, key=None)
    provider.initialize("s1", platform="cli")
    assert provider.prefetch("how do we deploy?", session_id="s1") == ""
    provider.queue_prefetch("how do we deploy?", session_id="s1")
    assert call(provider, "specivo_remember", text="x")["ok"] is False
    assert provider.system_prompt_block() == ""
    assert stub.requests == []


# -- prefetch ------------------------------------------------------------------------------------


def test_first_turn_prefetch_hit(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE, WIKI]
    provider = ready(stub)
    text = provider.prefetch("why does the backup check fail?", session_id="s1")
    lines = text.splitlines()
    assert lines[0].startswith("Specivo knowledge for project ACME (2 hits;")
    assert lines[1] == f'- [issue] ACME-12 Backup check fails: "the backup" {stub.url}/issue/ACME-12/'
    assert lines[2].endswith(f"{stub.url}/projects/ACME/wiki/deployment-checklist/")
    status = provider.recall_status()
    assert status is not None and (status.provider_label, status.count) == ("Specivo", 2)
    (request,) = stub.calls("GET", "/api/v1/search/")
    assert request.query == {
        "q": "why does the backup check fail?",
        "project_key": "ACME",
        "mode": "hybrid",
        "scope": "all",
        "limit": "5",
    }
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_calls_name_the_hermes_session(stub: StubSpecivo) -> None:
    """Specivo files the provider's REST calls under the Hermes session, beside its MCP calls and events."""
    stub.search_items = [ISSUE]
    provider = ready(stub)
    provider.prefetch("why does the backup check fail?", session_id="s1")
    assert {r.headers.get("X-Agent-Session-Id") for r in stub.calls("GET", "/api/v1/search/")} == {"s1"}

    provider.on_session_switch("20260929_165300_77812665")
    provider.prefetch("and the restore check?", session_id="20260929_165300_77812665")
    assert stub.calls("GET", "/api/v1/search/")[-1].headers.get("X-Agent-Session-Id") == "20260929_165300_77812665"

    provider.on_session_switch("../not an id")
    provider.prefetch("what about the logs?", session_id="x")
    assert "X-Agent-Session-Id" not in stub.calls("GET", "/api/v1/search/")[-1].headers


def test_prefetch_empty(stub: StubSpecivo) -> None:
    provider = ready(stub)
    assert provider.prefetch("anything about invoices?", session_id="s1") == ""
    assert provider.recall_status() is None
    assert len(stub.calls("GET", "/api/v1/search/")) == 1


def test_prefetch_timeout_returns_empty_quickly(stub: StubSpecivo, caplog: pytest.LogCaptureFixture) -> None:
    stub.search_items, stub.delay = [ISSUE], 1.0
    provider = ready(stub, first_turn_timeout=0.2)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert provider.prefetch("why does the backup check fail?", session_id="s1") == ""
    assert time.monotonic() - started < 0.9
    assert "longer than 0.2s" in caplog.text
    assert provider.recall_status() is None
    for _ in range(200):  # the late answer is kept for the next turn
        state = provider._sessions.get("s1")
        if state is not None and state.pending is not None:
            break
        time.sleep(0.01)
    stub.delay = 0.0
    assert "ACME-12" in provider.prefetch("a follow-up question", session_id="s1")


@pytest.mark.parametrize("status", [500, 403])
def test_prefetch_error_fails_soft(stub: StubSpecivo, status: int, caplog: pytest.LogCaptureFixture) -> None:
    stub.refuse[("GET", "/api/v1/search/")] = status
    provider = ready(stub)
    with caplog.at_level(logging.WARNING):
        assert provider.prefetch("why does the backup check fail?", session_id="s1") == ""
    assert f"answered {status}" in caplog.text


def test_prefetch_with_specivo_down(stub: StubSpecivo, caplog: pytest.LogCaptureFixture) -> None:
    stub.url = "http://127.0.0.1:9"  # nothing listens on the discard port
    down = ready(stub)
    with caplog.at_level(logging.WARNING):
        assert down.prefetch("why does the backup check fail?", session_id="s1") == ""
        result = call(down, "specivo_remember", text="Backups run at 02:00 UTC.")
    assert "unreachable" in caplog.text
    assert result["ok"] is False and "unreachable" in result["error"]


@pytest.mark.parametrize("prompt", ["ok", "thanks!", "/specivo issue ACME-1", "   "])
def test_trivial_prompts_and_slash_commands_are_not_searched(stub: StubSpecivo, prompt: str) -> None:
    provider = ready(stub)
    assert provider.prefetch(prompt, session_id="s1") == ""
    provider.queue_prefetch(prompt, session_id="s1")
    time.sleep(0.05)
    assert stub.requests == []


def test_queue_prefetch_caches_per_session(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE]
    provider = ready(stub)
    provider.queue_prefetch("backup check", session_id="a")
    stub.wait_for("GET", "/api/v1/search/")
    for _ in range(100):  # the worker stores its hits right after the answer
        state = provider._sessions.get("a")
        if state is not None and state.pending is not None:
            break
        time.sleep(0.01)
    stub.search_items = []
    assert "ACME-12" in provider.prefetch("a new question", session_id="a")
    assert len(stub.calls("GET", "/api/v1/search/")) == 1  # served from the cache, no search for this turn
    assert provider.prefetch("a third question", session_id="a") == ""  # consumed; nothing queued
    assert provider.prefetch("first turn of b", session_id="b") == ""  # b's first turn searches for itself
    assert len(stub.calls("GET", "/api/v1/search/")) == 2


def test_repeated_query_drops_hits_already_shown(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE, WIKI]
    provider = ready(stub)
    assert "(2 hits;" in provider.prefetch("backup check", session_id="s1")
    provider._background_recall("s1", "backup check", 2.0)
    assert provider.prefetch("backup check", session_id="s1") == ""
    provider._background_recall("s1", "deployment", 2.0)
    assert "(2 hits;" in provider.prefetch("deployment", session_id="s1")


def test_query_is_trimmed_and_redacted(stub: StubSpecivo) -> None:
    provider = ready(stub)
    provider.prefetch(f"token {CANARY} " + "word " * 200, session_id="s1")
    (request,) = stub.calls("GET", "/api/v1/search/")
    assert CANARY not in request.query["q"] and "«redacted»" in request.query["q"]
    assert len(request.query["q"]) <= 300


def test_no_recall_when_redaction_is_unavailable(stub: StubSpecivo, caplog: pytest.LogCaptureFixture) -> None:
    provider = ready(stub, redactor=lambda text: REDACTION_UNAVAILABLE)
    with caplog.at_level(logging.WARNING):
        assert provider.prefetch("why does the backup check fail?", session_id="s1") == ""
    assert stub.requests == [] and "redaction" in caplog.text


def test_cron_context_still_recalls(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE]
    provider = ready(stub, ctx="cron")
    assert "ACME-12" in provider.prefetch("morning briefing on backups", session_id="s1")


def test_sync_turn_stores_nothing(stub: StubSpecivo) -> None:
    provider = ready(stub)
    provider.sync_turn(f"my key is {CANARY}", "noted", session_id="s1", messages=[{"role": "user"}])
    time.sleep(0.05)
    assert stub.requests == []


# -- remember ------------------------------------------------------------------------------------


def test_remember_creates_then_appends_to_the_notes_page(stub: StubSpecivo) -> None:
    provider = ready(stub)
    first = call(provider, "specivo_remember", text="Backups run at 02:00 UTC.", kind="fact")
    assert first == {
        "ok": True,
        "url": f"{stub.url}/projects/ACME/wiki/agent-notes/",
        "project": "ACME",
        "page": "Agent Notes",
    }
    second = call(provider, "specivo_remember", text="We keep\nseven daily backups.", kind="decision")
    assert second["ok"] is True
    page = stub.pages["ACME"]["agent-notes"]
    assert page["text"] == (
        "## 2026-01-15\n\n"
        "- 2026-01-15 09:30 UTC [fact] Backups run at 02:00 UTC.\n"
        "- 2026-01-15 09:30 UTC [decision] We keep seven daily backups.\n"
    )
    (create,) = stub.calls("POST", "/api/v1/projects/ACME/wiki/")
    assert create.body["title"] == "Agent Notes" and create.body["comments"] == "Agent note"
    (patch,) = stub.calls("PATCH")
    assert patch.body["lock_version"] == 1


def test_remember_uses_the_configured_page(stub: StubSpecivo) -> None:
    provider = ready(stub, {"notes_page": "Ops Log"})
    assert call(provider, "specivo_remember", text="Disk alert threshold is 80%.")["url"].endswith("/wiki/ops-log/")


def test_remember_redacts_before_sending(stub: StubSpecivo) -> None:
    ctx = FakeCtx()
    provider = ready(stub, redactor=Runtime(ctx).redact)
    assert call(provider, "specivo_remember", text=f"The staging token is {CANARY}.")["ok"] is True
    assert call(provider, "specivo_remember", text=f"Rotate {CANARY}.", issue_ref="ACME-12")["ok"] is True
    sent = json.dumps([r.body for r in stub.requests], ensure_ascii=False)
    assert CANARY not in sent and "«redacted»" in sent
    assert any(CANARY in text for text in ctx.redacted)


def test_remember_is_skipped_when_redaction_is_unavailable(stub: StubSpecivo, caplog: pytest.LogCaptureFixture) -> None:
    provider = ready(stub, redactor=lambda text: REDACTION_UNAVAILABLE)
    with caplog.at_level(logging.WARNING):
        result = call(provider, "specivo_remember", text="Backups run at 02:00 UTC.")
    assert result["ok"] is False and "redaction is unavailable" in result["error"]
    assert stub.requests == [] and "not saved" in caplog.text


def test_remember_without_hermes_redactor_fails_closed(stub: StubSpecivo) -> None:
    """The default redactor needs Hermes's ``agent.redact``; without it nothing is written."""
    raw = {"url": stub.url, "memory": {"project": "ACME"}}
    provider = SpecivoMemoryProvider(config=lambda name, default: raw.get(name, default), secret=lambda name: KEY)
    provider.initialize("s1", platform="cli")
    assert call(provider, "specivo_remember", text="Backups run at 02:00 UTC.")["ok"] is False
    assert stub.requests == []


def test_remember_retries_once_on_an_edit_conflict(stub: StubSpecivo) -> None:
    stub.pages["ACME"] = {"agent-notes": {"title": "Agent Notes", "slug": "agent-notes", "text": "", "lock_version": 4}}
    stub.conflicts = 1
    provider = ready(stub)
    assert call(provider, "specivo_remember", text="Backups run at 02:00 UTC.")["ok"] is True
    assert len(stub.calls("PATCH")) == 2
    stub.conflicts = 2
    result = call(provider, "specivo_remember", text="Another note.")
    assert result["ok"] is False and "changed by someone else" in result["error"]


@pytest.mark.parametrize(
    ("status", "message"),
    [(403, "may not edit the wiki in ACME"), (401, "refused the API key"), (404, "not visible")],
)
def test_remember_permission_refusal(stub: StubSpecivo, status: int, message: str) -> None:
    # A missing page (404 on read) is created; a project the key cannot see refuses the create too.
    stub.refuse[("GET", "/api/v1/projects/ACME/wiki/")] = status
    stub.refuse[("POST", "/api/v1/projects/ACME/wiki/")] = status
    result = call(ready(stub), "specivo_remember", text="Backups run at 02:00 UTC.")
    assert result["ok"] is False and message in result["error"]


def test_remember_comment_refused(stub: StubSpecivo) -> None:
    stub.refuse[("POST", "/api/v1/issues/")] = 403
    result = call(ready(stub), "specivo_remember", text="Checked the logs.", issue_ref="ACME-12")
    assert result == {"ok": False, "error": "not saved: the API key's account may not comment on ACME-12"}


def test_remember_on_an_issue_posts_a_comment(stub: StubSpecivo) -> None:
    result = call(ready(stub), "specivo_remember", text="Root cause: a full disk.", kind="fact", issue_ref="ACME-12")
    assert result == {"ok": True, "url": f"{stub.url}/issue/ACME-12/", "issue": "ACME-12"}
    assert stub.comments == [("ACME-12", "[fact] Root cause: a full disk.")]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "text is required"),
        ({"text": "  "}, "text is required"),
        ({"text": "x" * 2001}, "longer than 2000"),
        ({"text": "ok", "kind": "secret"}, "kind must be"),
        ({"text": "ok", "issue_ref": "not a key"}, "issue key"),
    ],
)
def test_remember_validates_arguments(stub: StubSpecivo, args: dict[str, Any], message: str) -> None:
    result = call(ready(stub), "specivo_remember", **args)
    assert result["ok"] is False and message in result["error"]
    assert stub.requests == []


@pytest.mark.parametrize("agent_context", ["cron", "subagent", "flush"])
def test_remember_is_refused_outside_primary_sessions(stub: StubSpecivo, agent_context: str) -> None:
    result = call(ready(stub, ctx=agent_context), "specivo_remember", text="Backups run at 02:00 UTC.")
    assert result["ok"] is False and agent_context in result["error"]
    assert stub.requests == []


def test_remember_without_a_project_explains(stub: StubSpecivo) -> None:
    raw: dict[str, Any] = {"url": stub.url}
    provider = SpecivoMemoryProvider(
        config=lambda name, default: raw.get(name, default),
        redactor=Runtime(FakeCtx()).redact,
        secret=lambda name: KEY,
    )
    provider.initialize("s1", platform="cli", cwd="/srv/unmapped")
    result = call(provider, "specivo_remember", text="Backups run at 02:00 UTC.")
    assert result["ok"] is False and "memory.project" in result["error"]
    assert provider.prefetch("why does the backup check fail?", session_id="s1") == ""
    assert stub.requests == []


# -- recall tool and schemas ---------------------------------------------------------------------


def test_recall_tool(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE, WIKI]
    provider = ready(stub)
    result = call(provider, "specivo_recall", query="backup", limit=1)
    assert result["ok"] is True and result["count"] == 1 and "ACME-12" in result["result"]
    assert call(provider, "specivo_recall", query="backup", scope="all")["count"] == 2
    project_scoped, everywhere = stub.calls("GET", "/api/v1/search/")
    assert project_scoped.query["project_key"] == "ACME" and project_scoped.query["limit"] == "1"
    assert "project_key" not in everywhere.query
    stub.search_items = []
    assert call(provider, "specivo_recall", query="nothing")["result"] == "No Specivo results in project ACME."
    assert call(provider, "specivo_recall", query="x", limit=11)["ok"] is False
    assert call(provider, "specivo_recall", query="x", scope="team")["ok"] is False
    assert call(provider, "specivo_recall", query=" ")["ok"] is False


def test_unknown_tool_and_schemas(stub: StubSpecivo) -> None:
    provider = ready(stub)
    assert [s["name"] for s in provider.get_tool_schemas()] == ["specivo_remember", "specivo_recall"]
    assert call(provider, "memory")["ok"] is False
    assert "ACME" in provider.system_prompt_block() and provider.name == "specivo"


# -- mirroring of built-in memory writes ---------------------------------------------------------


def test_mirroring_is_off_by_default(stub: StubSpecivo) -> None:
    provider = ready(stub)
    provider.on_memory_write("add", "memory", "The user prefers short answers.")
    time.sleep(0.1)
    assert stub.requests == []


def test_mirroring_when_enabled(stub: StubSpecivo) -> None:
    provider = ready(stub, {"mirror_builtin": True})
    provider.on_memory_write("add", "user", f"Uses {CANARY} for staging.")
    provider.on_memory_write("remove", "memory", "anything")  # removals are not mirrored
    stub.wait_for("POST", "/api/v1/projects/ACME/wiki/")
    for _ in range(100):
        if "agent-notes" in stub.pages.get("ACME", {}):
            break
        time.sleep(0.01)
    text = stub.pages["ACME"]["agent-notes"]["text"]
    assert text == "## Memory mirror\n\n- 2026-01-15 09:30 UTC [user add] Uses «redacted» for staging.\n"


# -- lifecycle and setup -------------------------------------------------------------------------


def test_session_switch_rebinds_and_reset_forgets(stub: StubSpecivo) -> None:
    stub.search_items = [ISSUE]
    provider = ready(stub)
    provider._background_recall("s2", "backup", 2.0)
    provider.on_session_switch("s2", reset=True)
    assert "s2" not in provider._sessions
    provider.shutdown()
    assert provider._sessions == {}


def test_save_config_validates_then_writes(monkeypatch: pytest.MonkeyPatch, stub: StubSpecivo) -> None:
    written: list[tuple[str, Any]] = []
    monkeypatch.setattr(provider_module, "save_memory_setting", lambda key, value: written.append((key, value)))
    provider = make_provider(stub)
    assert [f["key"] for f in provider.get_config_schema()] == ["project", "notes_page", "max_hits", "max_chars"]
    provider.save_config({"project": "acme", "max_hits": "3", "notes_page": "", "unrelated": 1}, "/tmp/hermes-home")
    assert written == [("project", "ACME"), ("max_hits", 3)]
    with pytest.raises(ConfigError, match="max_chars"):
        provider.save_config({"max_chars": 10}, "/tmp/hermes-home")
    assert len(written) == 2

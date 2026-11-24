from __future__ import annotations

import json

import pytest
from conftest import Clock, FakeCtx

from hermes_specivo.runtime import Runtime
from hermes_specivo.session_link import LINK_RETRY_SECONDS, MAX_SESSIONS, SessionLink, mcp_tool_prefix, valid_session_id

SID = "20261123_101500_ab12cd"
PARENT = "20261123_100000_ffee00"
TOOL = "mcp__specivo__"


@pytest.fixture
def link(runtime: Runtime) -> SessionLink:
    return SessionLink(runtime)


def test_first_turn_sets_the_external_session_id_and_parent(link: SessionLink, ctx: FakeCtx) -> None:
    assert link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT, is_first_turn=True, model="m") is None
    assert ctx.mcp.calls == [
        ("specivo", "specivo_agent_session", {"op": "set", "external_session_id": SID, "parent_session_id": PARENT})
    ]


def test_every_turn_selects_the_session_and_only_the_first_sends_parent_and_issue(
    link: SessionLink, ctx: FakeCtx
) -> None:
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_timer", args={"op": "start", "issue_ref": "ACME-5"}, session_id=SID
    )
    link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT, is_first_turn=True)
    link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT, is_first_turn=False)
    link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT, is_first_turn=False)
    assert ctx.mcp.tool_calls("specivo_agent_session") == [
        {"op": "set", "external_session_id": SID, "parent_session_id": PARENT, "issue_ref": "ACME-5"},
        {"op": "set", "external_session_id": SID},
        {"op": "set", "external_session_id": SID},
    ]


def test_no_parent_is_omitted_not_sent_as_null(link: SessionLink, ctx: FakeCtx) -> None:
    link.on_pre_llm_call(session_id=SID, is_first_turn=True)
    assert ctx.mcp.tool_calls("specivo_agent_session") == [{"op": "set", "external_session_id": SID}]


def test_sessions_sharing_a_connection_each_select_their_own(link: SessionLink, ctx: FakeCtx) -> None:
    """A subagent's turn between two parent turns: each turn re-selects its own session row."""
    link.on_pre_llm_call(session_id=SID, is_first_turn=True)
    link.on_pre_llm_call(session_id=PARENT, is_first_turn=True)
    link.on_pre_llm_call(session_id=SID)
    selected = [call["external_session_id"] for call in ctx.mcp.tool_calls("specivo_agent_session")]
    assert selected == [SID, PARENT, SID]


def test_a_resumed_session_is_linked_on_its_first_seen_turn(link: SessionLink, ctx: FakeCtx) -> None:
    link.on_pre_llm_call(session_id=SID, is_first_turn=False)
    assert ctx.mcp.tool_calls("specivo_agent_session") == [{"op": "set", "external_session_id": SID}]


def test_a_failed_link_is_retried_after_a_pause(link: SessionLink, ctx: FakeCtx, clock: Clock) -> None:
    ctx.mcp.handlers["specivo_agent_session"] = RuntimeError("No agent session is tracked for this call")
    link.on_pre_llm_call(session_id=SID, is_first_turn=True)
    ctx.mcp.handlers["specivo_agent_session"] = lambda a: "Agent session updated."
    link.on_pre_llm_call(session_id=SID)  # within the pause: no call, the turn is not slowed down
    assert len(ctx.mcp.tool_calls("specivo_agent_session")) == 1
    clock.now += LINK_RETRY_SECONDS
    link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT)
    link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT)
    assert ctx.mcp.tool_calls("specivo_agent_session")[1:] == [
        {"op": "set", "external_session_id": SID, "parent_session_id": PARENT},  # still unlinked: full set
        {"op": "set", "external_session_id": SID},  # linked: a plain select
    ]


def test_a_failed_select_after_linking_also_pauses(link: SessionLink, ctx: FakeCtx, clock: Clock) -> None:
    link.on_pre_llm_call(session_id=SID, is_first_turn=True)
    ctx.mcp.handlers["specivo_agent_session"] = RuntimeError("unreachable")
    link.on_pre_llm_call(session_id=SID)
    link.on_pre_llm_call(session_id=SID)
    assert len(ctx.mcp.tool_calls("specivo_agent_session")) == 2
    clock.now += LINK_RETRY_SECONDS
    ctx.mcp.handlers["specivo_agent_session"] = lambda a: "Agent session selected."
    link.on_pre_llm_call(session_id=SID)
    assert ctx.mcp.tool_calls("specivo_agent_session")[-1] == {"op": "set", "external_session_id": SID}


def test_nothing_happens_before_setup(clock: Clock) -> None:
    ctx = FakeCtx({})
    link = SessionLink(Runtime(ctx, clock=clock))
    assert link.on_pre_llm_call(session_id=SID, is_first_turn=True) is None
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_timer", args={"op": "start", "issue_ref": "ACME-1"}, session_id=SID
    )
    assert ctx.mcp.calls == []
    assert ctx.state.data == {}


def test_missing_allowlist_is_reported_not_raised(link: SessionLink, ctx: FakeCtx) -> None:
    ctx.mcp.allowlist = set()
    assert link.on_pre_llm_call(session_id=SID, is_first_turn=True) is None


@pytest.mark.parametrize("bad", ["", "has space", "x" * 201, "semi;colon"])
def test_invalid_session_ids_are_skipped(link: SessionLink, ctx: FakeCtx, bad: str) -> None:
    assert valid_session_id(bad) is None
    link.on_pre_llm_call(session_id=bad, is_first_turn=True)
    assert ctx.mcp.calls == []


def test_status_change_sets_the_current_issue_and_the_context_line(link: SessionLink, ctx: FakeCtx) -> None:
    link.on_pre_llm_call(session_id=SID, is_first_turn=True)
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_update_issue",
        args={"issue_ref": "ACME-12", "status_id": 2},
        result=json.dumps({"result": "Updated issue ACME-12"}),
        session_id=SID,
        status="ok",
    )
    assert link.current_issue(SID) == ("ACME-12", "In Progress")
    assert link.on_pre_llm_call(session_id=SID) == {"context": "Current Specivo issue: ACME-12 (In Progress)"}


def test_assignee_change_sets_the_issue_without_a_status(link: SessionLink) -> None:
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_update_issue", args={"issue_ref": "ACME-4", "assigned_to_id": 7}, session_id=SID
    )
    assert link.current_issue(SID) == ("ACME-4", None)


def test_a_comment_only_update_does_not_change_the_issue(link: SessionLink) -> None:
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_update_issue", args={"issue_ref": "ACME-4", "notes": "hi"}, session_id=SID
    )
    assert link.current_issue(SID) == (None, None)


def test_resolving_the_current_issue_clears_it_here_and_on_specivo(link: SessionLink, ctx: FakeCtx) -> None:
    update = TOOL + "specivo_update_issue"
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-12", "status_id": 2}, session_id=SID)
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-12", "status_id": 3}, session_id=SID)
    assert link.current_issue(SID) == (None, None)
    assert ctx.mcp.tool_calls("specivo_agent_session") == [
        {"op": "set", "external_session_id": SID, "issue_ref": ""}  # the empty string is sent, not dropped
    ]
    assert link.on_pre_llm_call(session_id=SID) is None


def test_a_failed_clear_on_resolve_still_clears_the_cache(link: SessionLink, ctx: FakeCtx) -> None:
    update = TOOL + "specivo_update_issue"
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-12", "status_id": 2}, session_id=SID)
    ctx.mcp.handlers["specivo_agent_session"] = RuntimeError("unreachable")
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-12", "status_id": 5}, session_id=SID)
    assert link.current_issue(SID) == (None, None)


def test_resolving_another_issue_keeps_the_current_one(link: SessionLink, ctx: FakeCtx) -> None:
    update = TOOL + "specivo_update_issue"
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-12", "status_id": 2}, session_id=SID)
    link.on_post_tool_call(tool_name=update, args={"issue_ref": "ACME-9", "status_id": 5}, session_id=SID)
    assert link.current_issue(SID) == ("ACME-12", "In Progress")
    assert ctx.mcp.tool_calls("specivo_agent_session") == []


def test_the_status_list_is_read_once_and_cached(link: SessionLink, ctx: FakeCtx) -> None:
    update = TOOL + "specivo_update_issue"
    for ref in ("ACME-1", "ACME-2", "ACME-3"):
        link.on_post_tool_call(tool_name=update, args={"issue_ref": ref, "status_id": 2}, session_id=SID)
    assert len(ctx.mcp.tool_calls("specivo_list_lookups")) == 1


def test_timer_start_and_explicit_set_mark_the_issue(link: SessionLink) -> None:
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_timer", args={"op": "start", "issue_ref": "ACME-5"}, session_id=SID
    )
    assert link.current_issue(SID) == ("ACME-5", None)
    link.on_post_tool_call(tool_name=TOOL + "specivo_timer", args={"op": "stop", "issue_ref": "ACME-6"}, session_id=SID)
    assert link.current_issue(SID) == ("ACME-5", None)
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_agent_session", args={"op": "set", "issue_ref": "ACME-7"}, session_id=SID
    )
    assert link.current_issue(SID) == ("ACME-7", None)


@pytest.mark.parametrize("empty", ["", "  "])
def test_an_explicit_set_with_an_empty_issue_clears_it(link: SessionLink, empty: str) -> None:
    tool = TOOL + "specivo_agent_session"
    link.on_post_tool_call(tool_name=tool, args={"op": "set", "issue_ref": "ACME-7"}, session_id=SID)
    link.on_post_tool_call(tool_name=tool, args={"op": "get", "issue_ref": empty}, session_id=SID)
    assert link.current_issue(SID) == ("ACME-7", None)  # only a set clears
    link.on_post_tool_call(tool_name=tool, args={"op": "set", "issue_ref": empty}, session_id=SID, status="error")
    assert link.current_issue(SID) == ("ACME-7", None)  # a failed call changes nothing
    link.on_post_tool_call(tool_name=tool, args={"op": "set", "issue_ref": empty}, session_id=SID)
    assert link.current_issue(SID) == (None, None)


def test_an_explicit_set_without_issue_ref_keeps_it(link: SessionLink) -> None:
    tool = TOOL + "specivo_agent_session"
    link.on_post_tool_call(tool_name=tool, args={"op": "set", "issue_ref": "ACME-7"}, session_id=SID)
    link.on_post_tool_call(tool_name=tool, args={"op": "set", "external_session_id": SID}, session_id=SID)
    assert link.current_issue(SID) == ("ACME-7", None)


def test_a_created_issue_becomes_current(link: SessionLink) -> None:
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_create_issue",
        args={"project_key": "ACME", "subject": "Fix the login form"},
        result=json.dumps({"result": "Created issue ACME-40: Fix the login form\nStatus: 1, Priority: 2"}),
        session_id=SID,
        status="ok",
    )
    assert link.current_issue(SID) == ("ACME-40", None)


def test_failed_calls_and_other_servers_are_ignored(link: SessionLink) -> None:
    args = {"op": "start", "issue_ref": "ACME-5"}
    link.on_post_tool_call(tool_name=TOOL + "specivo_timer", args=args, session_id=SID, status="error")
    link.on_post_tool_call(tool_name="mcp__other__specivo_timer", args=args, session_id=SID)
    link.on_post_tool_call(tool_name="terminal", args=args, session_id=SID)
    assert link.current_issue(SID) == (None, None)


def test_a_renamed_server_is_matched(clock: Clock) -> None:
    ctx = FakeCtx({"url": "https://specivo.example.test", "mcp_server": "work-tracker"})
    link = SessionLink(Runtime(ctx, clock=clock))
    assert mcp_tool_prefix("work-tracker") == "mcp__work_tracker__"
    link.on_post_tool_call(
        tool_name="mcp__work_tracker__specivo_timer", args={"op": "start", "issue_ref": "ACME-5"}, session_id=SID
    )
    assert link.current_issue(SID) == ("ACME-5", None)


def test_context_injection_can_be_switched_off(clock: Clock) -> None:
    ctx = FakeCtx({"url": "https://specivo.example.test", "context": {"inject": False}})
    link = SessionLink(Runtime(ctx, clock=clock))
    link.on_post_tool_call(
        tool_name=TOOL + "specivo_timer", args={"op": "start", "issue_ref": "ACME-5"}, session_id=SID
    )
    assert link.on_pre_llm_call(session_id=SID) is None
    assert ctx.mcp.tool_calls("specivo_agent_session")  # the link itself still happens


def test_slash_issue_sets_it_on_specivo_and_locally(link: SessionLink, ctx: FakeCtx) -> None:
    assert link.command("issue acme-12", session_id=SID) == "Current Specivo issue: ACME-12"
    assert ctx.mcp.tool_calls("specivo_agent_session") == [
        {"op": "set", "external_session_id": SID, "issue_ref": "ACME-12"}
    ]
    assert link.current_issue(SID) == ("ACME-12", None)
    # Already linked: the next turn only selects the session, and it carries the context line.
    assert link.on_pre_llm_call(session_id=SID, parent_session_id=PARENT) == {
        "context": "Current Specivo issue: ACME-12"
    }
    assert ctx.mcp.tool_calls("specivo_agent_session")[1:] == [{"op": "set", "external_session_id": SID}]


def test_slash_issue_reports_a_refusal(link: SessionLink, ctx: FakeCtx) -> None:
    ctx.mcp.handlers["specivo_agent_session"] = RuntimeError("Issue ACME-99 not found")
    assert "not found" in link.command("issue ACME-99", session_id=SID)
    assert link.current_issue(SID) == (None, None)


def test_slash_issue_clear_and_bad_input(link: SessionLink, ctx: FakeCtx) -> None:
    link.command("issue ACME-12", session_id=SID)
    assert "cleared" in link.command("issue clear", session_id=SID)
    assert ctx.mcp.tool_calls("specivo_agent_session")[-1] == {
        "op": "set",
        "external_session_id": SID,
        "issue_ref": "",  # the empty string is sent, not dropped
    }
    assert link.current_issue(SID) == (None, None)
    assert "not an issue key" in link.command("issue twelve", session_id=SID)
    assert "No session" in link.command("issue ACME-1", session_id=None)
    assert link.command("", session_id=SID).startswith("Usage")


def test_slash_issue_clear_reports_a_failure_and_still_forgets_locally(link: SessionLink, ctx: FakeCtx) -> None:
    link.command("issue ACME-12", session_id=SID)
    ctx.mcp.handlers["specivo_agent_session"] = RuntimeError("unreachable")
    text = link.command("issue clear", session_id=SID)
    assert "cleared" in text and "unreachable" in text
    assert link.current_issue(SID) == (None, None)


def test_slash_status_shows_both_sides(link: SessionLink, ctx: FakeCtx) -> None:
    link.command("issue ACME-12", session_id=SID)
    ctx.mcp.handlers["specivo_agent_session"] = lambda a: f"external_session_id: {a.get('external_session_id')}"
    text = link.command("status", session_id=SID)
    assert f"Hermes session: {SID}" in text
    assert "Current issue (this plugin): ACME-12" in text
    assert "Specivo agent session:" in text
    assert ctx.mcp.tool_calls("specivo_agent_session")[-1] == {"op": "get", "external_session_id": SID}
    assert f"  external_session_id: {SID}" in text


def test_slash_status_without_a_session_shows_the_connection(link: SessionLink, ctx: FakeCtx) -> None:
    link.command("status", session_id=None)
    assert ctx.mcp.tool_calls("specivo_agent_session") == [{"op": "get"}]


def test_slash_before_setup(clock: Clock) -> None:
    link = SessionLink(Runtime(FakeCtx({}), clock=clock))
    assert "not set up" in link.command("status", session_id=SID)
    bad = SessionLink(Runtime(FakeCtx({"url": "http://specivo.example.test"}), clock=clock))
    assert "invalid" in bad.command("status", session_id=SID)


def test_session_cache_is_bounded(link: SessionLink, ctx: FakeCtx, clock: Clock) -> None:
    for n in range(MAX_SESSIONS + 5):
        clock.now += 1
        link.command(f"issue ACME-{n + 1}", session_id=f"s{n}")
    link.on_session_end(session_id="s0")
    sessions = ctx.state.data["sessions"]
    assert len(sessions) == MAX_SESSIONS
    assert "s0" not in sessions and f"s{MAX_SESSIONS + 4}" in sessions

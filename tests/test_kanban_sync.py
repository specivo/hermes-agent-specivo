from __future__ import annotations

from typing import Any

import pytest
from conftest import BASE_URL, Clock, FakeCtx, issue_list

from hermes_specivo.kanban_sync import MARKER, MAX_NEW_CARDS_PER_POLL, SIGNATURE, KanbanSync, card_signature
from hermes_specivo.runtime import REDACTION_UNAVAILABLE, Runtime

SYNC_ON: dict[str, Any] = {"enabled": True, "poll_seconds": 60}
API_KEY = "spv_test_key"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin signs its cards with a key derived from SPECIVO_API_KEY (a required secret)."""
    monkeypatch.setenv("SPECIVO_API_KEY", API_KEY)


def make(clock: Clock, issue_sync: dict[str, Any] | None = None, **settings: Any) -> tuple[FakeCtx, KanbanSync]:
    ctx = FakeCtx({"url": BASE_URL, "default_project": "ACME", "issue_sync": issue_sync or SYNC_ON, **settings})
    return ctx, KanbanSync(Runtime(ctx, clock=clock))


def serve(ctx: FakeCtx, rows: list[tuple[str, str, str]], total: int | None = None) -> None:
    ctx.mcp.handlers["specivo_list_issues"] = lambda a: issue_list(a["project_key"], rows, total)


def tick(sync: KanbanSync, clock: Clock, **payload: Any) -> None:
    clock.now += 61
    sync.on_kanban_dispatch_tick(board=payload.pop("board", None), profile_name="worker", outcome="idle", **payload)


def task_of(sync: KanbanSync, key: str) -> str:
    return str(sync.snapshot()["cards"][key]["task_id"])


def test_sync_is_off_by_default(clock: Clock) -> None:
    ctx = FakeCtx({"url": BASE_URL, "default_project": "ACME"})
    sync = KanbanSync(Runtime(ctx, clock=clock))
    tick(sync, clock)
    sync.on_task_claimed(task_id="t_1")
    assert ctx.mcp.calls == [] and ctx.board.calls == []


def test_new_open_issues_become_cards(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "Fix the login form"), ("ACME-2", "In Progress", "Add export")])
    tick(sync, clock)
    assert ctx.mcp.tool_calls("specivo_list_issues") == [
        {
            "project_key": "ACME",
            "status": "open",
            "assignee": "me",
            "offset": 0,
            "limit": 100,
            "sort": "created_at:asc",
        }
    ]
    created = ctx.board.created()
    # Only the key: the subject is untrusted text and the card goes to an autonomous worker.
    assert [c["title"] for c in created] == ["Specivo issue ACME-1", "Specivo issue ACME-2"]
    assert not any("Fix the login form" in c["title"] + c["body"] for c in created)
    first = created[0]
    assert first["assignee"] == "worker"  # the plugin's own profile by default
    assert first["idempotency_key"] == "specivo:ACME-1"
    assert f"{BASE_URL}/issue/ACME-1/" in first["body"]
    assert f"{MARKER} ACME-1 sig={card_signature(API_KEY, 'ACME-1')}" in first["body"]
    assert "Fix the login form" not in first["body"]  # a reference, not the issue text
    assert sync.snapshot()["cards"]["ACME-1"] == {"task_id": "t_1", "state": "open", "board": None}


def test_the_poll_is_throttled(clock: Clock) -> None:
    ctx, sync = make(clock)
    tick(sync, clock)
    clock.now += 10
    sync.on_kanban_dispatch_tick(board=None)
    assert len(ctx.mcp.tool_calls("specivo_list_issues")) == 1
    tick(sync, clock)
    assert len(ctx.mcp.tool_calls("specivo_list_issues")) == 2


def test_dry_run_ticks_and_other_boards_are_ignored(clock: Clock) -> None:
    ctx, sync = make(clock, {**SYNC_ON, "board": "ops"})
    tick(sync, clock, dry_run=True)
    tick(sync, clock, board="research")
    assert ctx.mcp.calls == []
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock, board="ops")
    assert ctx.board.created()[0]["board"] == "ops"


def test_selector_tag_and_card_assignee(clock: Clock) -> None:
    ctx, sync = make(clock, {**SYNC_ON, "project": "OPS", "select": {"tag": "agent"}, "card_assignee": "coder"})
    serve(ctx, [("OPS-3", "New", "Rotate logs")])
    tick(sync, clock)
    call = ctx.mcp.tool_calls("specivo_list_issues")[0]
    # A tag narrows the selection; issues are always limited to the plugin's own account.
    assert call["project_key"] == "OPS" and call["tag"] == "agent" and call["assignee"] == "me"
    assert ctx.board.created()[0]["assignee"] == "coder"


def test_a_second_poll_creates_no_duplicates(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    tick(sync, clock)
    assert len(ctx.board.created()) == 1


def test_finished_issues_get_no_card(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "Resolved", "Done already")])
    tick(sync, clock)
    assert ctx.board.created() == []


def test_claim_moves_the_issue_to_in_progress_once(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    sync.on_task_claimed(task_id=task_of(sync, "ACME-1"), board=None, assignee="worker", run_id=1)
    sync.on_task_claimed(task_id=task_of(sync, "ACME-1"))
    assert ctx.mcp.tool_calls("specivo_update_issue") == [{"issue_ref": "ACME-1", "status_id": 2}]
    assert sync.snapshot()["cards"]["ACME-1"]["state"] == "claimed"


def test_completion_posts_the_redacted_summary_and_resolves(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    tid = task_of(sync, "ACME-1")
    sync.on_task_claimed(task_id=tid)
    sync.on_task_completed(task_id=tid, summary="Fixed it. Token sk-test-secret rotated.")
    sync.on_task_completed(task_id=tid, summary="again")
    updates = ctx.mcp.tool_calls("specivo_update_issue")
    assert updates[1] == {
        "issue_ref": "ACME-1",
        "status_id": 3,
        "notes": "Fixed it. Token «redacted» rotated." + SIGNATURE,
    }
    assert len(updates) == 2  # no double comment
    assert ctx.redacted == ["Fixed it. Token sk-test-secret rotated."]


def test_block_posts_the_reason_and_moves_to_feedback(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    tid = task_of(sync, "ACME-1")
    sync.on_task_claimed(task_id=tid)
    sync.on_task_blocked(task_id=tid, reason="Which database should the export read?")
    sync.on_task_blocked(task_id=tid, reason="same block fired twice")
    updates = ctx.mcp.tool_calls("specivo_update_issue")
    assert updates[1] == {
        "issue_ref": "ACME-1",
        "status_id": 4,
        "notes": "Blocked in Hermes: Which database should the export read?" + SIGNATURE,
    }
    assert len(updates) == 2
    # Unblocked and claimed again: back to In Progress.
    sync.on_task_claimed(task_id=tid)
    assert ctx.mcp.tool_calls("specivo_update_issue")[-1] == {"issue_ref": "ACME-1", "status_id": 2}


def test_an_issue_closed_in_specivo_completes_its_card_without_a_comment(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One"), ("ACME-2", "New", "Two")])
    tick(sync, clock)
    # kanban_complete fires kanban_task_completed in this process; the hook must stay quiet.
    ctx.board.on_complete = lambda tid, summary: sync.on_task_completed(task_id=tid, summary=summary)
    serve(ctx, [("ACME-2", "Resolved", "Two")])
    tick(sync, clock)
    completed = [args for tool, args in ctx.board.calls if tool == "kanban_complete"]
    assert completed == [
        {"task_id": "t_2", "summary": "ACME-2 is Resolved in Specivo."},
        {"task_id": "t_1", "summary": "ACME-1 is no longer open in Specivo."},
    ]
    assert ctx.mcp.tool_calls("specivo_update_issue") == []
    cards = sync.snapshot()["cards"]
    assert cards["ACME-1"]["state"] == "closed" and cards["ACME-2"]["state"] == "closed"
    # Next poll: ACME-1 is gone from Specivo's open list and its card is final, so it is forgotten.
    tick(sync, clock)
    assert "ACME-1" not in sync.snapshot()["cards"] and "t_1" not in sync.snapshot()["tasks"]


def test_a_partial_listing_never_closes_cards(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    calls: list[int] = []

    def page(args: dict[str, Any]) -> str:
        calls.append(args["offset"])
        start = args["offset"]
        rows = [(f"ACME-{n}", "New", f"Issue {n}") for n in range(100 + start, 100 + start + 100)]
        return issue_list("ACME", rows, total=900)

    ctx.mcp.handlers["specivo_list_issues"] = page
    tick(sync, clock)
    assert calls == [0, 100, 200, 300, 400]
    assert sync.snapshot()["cards"]["ACME-1"]["state"] == "open"
    assert len(ctx.board.created()) == 1 + MAX_NEW_CARDS_PER_POLL  # a big backlog fills the board gradually


def test_a_failed_update_is_retried_on_the_next_tick(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    tid = task_of(sync, "ACME-1")
    ctx.mcp.handlers["specivo_update_issue"] = RuntimeError("Service unavailable")
    sync.on_task_completed(task_id=tid, summary="Done")
    assert sync.snapshot()["cards"]["ACME-1"]["pending"] == {"status_id": 3, "notes": "Done" + SIGNATURE}
    ctx.mcp.handlers["specivo_update_issue"] = lambda a: "Updated"
    tick(sync, clock)
    assert ctx.mcp.tool_calls("specivo_update_issue")[-1] == {
        "issue_ref": "ACME-1",
        "status_id": 3,
        "notes": "Done" + SIGNATURE,
    }
    assert sync.snapshot()["cards"]["ACME-1"]["pending"] is None


def test_a_card_from_another_process_is_recognised_by_its_marker(clock: Clock) -> None:
    dispatcher, dispatcher_sync = make(clock)
    serve(dispatcher, [("ACME-1", "New", "One")])
    tick(dispatcher_sync, clock)
    # The worker process has its own (empty) state but sees the same board.
    worker, worker_sync = make(clock)
    worker.board = dispatcher.board
    serve(worker, [("ACME-1", "In Progress", "One")])
    worker_sync.on_task_completed(task_id="t_1", summary="Shipped")
    assert worker.mcp.tool_calls("specivo_update_issue") == [
        {"issue_ref": "ACME-1", "status_id": 3, "notes": "Shipped" + SIGNATURE}
    ]


def test_cards_the_plugin_did_not_make_are_left_alone(clock: Clock) -> None:
    ctx, sync = make(clock)
    ctx.board.tasks["t_9"] = {"title": "ACME-1: hand made", "body": "no marker here"}
    sync.on_task_completed(task_id="t_9", summary="Done")
    sync.on_task_completed(task_id="t_404", summary="Done")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []


@pytest.mark.parametrize(
    "body",
    [
        f"{MARKER} ACME-1",  # the old unsigned marker anyone can type
        f"{MARKER} ACME-1 sig=00000000000000000000000000000000",
        f"{MARKER} ACME-1 sig={card_signature('a-different-key', 'ACME-1')}",
        f"{MARKER} ACME-2 sig={card_signature(API_KEY, 'ACME-1')}",  # a signature copied from another issue
    ],
)
def test_a_forged_marker_is_not_adopted(clock: Clock, body: str) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "In Progress", "One"), ("ACME-2", "In Progress", "Two")])
    ctx.board.tasks["t_9"] = {"title": "Specivo issue ACME-1", "body": body, "idempotency_key": "specivo:ACME-1"}
    sync.on_task_claimed(task_id="t_9")
    sync.on_task_completed(task_id="t_9", summary="Done")
    sync.on_task_blocked(task_id="t_9", reason="Stop")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []
    assert sync.snapshot()["cards"] == {}


def test_a_signed_card_outside_the_sync_project_is_not_adopted(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("OTHER-1", "In Progress", "Elsewhere")])
    ctx.board.tasks["t_9"] = {"title": "x", "body": f"{MARKER} OTHER-1 sig={card_signature(API_KEY, 'OTHER-1')}"}
    sync.on_task_completed(task_id="t_9", summary="Done")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []


def test_a_signed_card_for_an_issue_outside_the_selection_is_not_adopted(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-2", "In Progress", "Mine")])  # ACME-1 is not assigned to the plugin's account
    ctx.board.tasks["t_9"] = {"title": "x", "body": f"{MARKER} ACME-1 sig={card_signature(API_KEY, 'ACME-1')}"}
    sync.on_task_completed(task_id="t_9", summary="Done")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []


def test_a_second_card_for_a_mapped_issue_is_not_adopted(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    copy = dict(ctx.board.tasks["t_1"])
    ctx.board.tasks["t_9"] = copy  # a copy of the real card, signature included
    sync.on_task_completed(task_id="t_9", summary="Done")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []
    assert sync.snapshot()["tasks"] == {"t_1": "ACME-1"}


def test_without_the_api_key_no_card_is_signed_or_adopted(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    monkeypatch.delenv("SPECIVO_API_KEY")
    tick(sync, clock)
    assert ctx.board.created() == []
    ctx.board.tasks["t_9"] = {"title": "x", "body": f"{MARKER} ACME-1 sig={card_signature(API_KEY, 'ACME-1')}"}
    sync.on_task_completed(task_id="t_9", summary="Done")
    assert ctx.mcp.tool_calls("specivo_update_issue") == []


@pytest.mark.parametrize("failure", ["missing", "raises", "not-a-string"])
def test_a_summary_is_withheld_when_redaction_is_unavailable(clock: Clock, failure: str) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)

    class Broken(FakeCtx):
        def redact_text(self, text: str, *, force: bool = False) -> str:
            if failure == "raises":
                raise RuntimeError("redactor crashed")
            return None  # type: ignore[return-value]

    broken = Broken(ctx.settings)
    broken.state, broken.mcp, broken.board = ctx.state, ctx.mcp, ctx.board
    if failure == "missing":
        broken.redact_text = None  # type: ignore[assignment,method-assign]
    worker = KanbanSync(Runtime(broken, clock=clock))
    worker.on_task_completed(task_id="t_1", summary="Token sk-test-secret rotated.")
    notes = ctx.mcp.tool_calls("specivo_update_issue")[-1]["notes"]
    assert "sk-test-secret" not in notes
    assert notes == REDACTION_UNAVAILABLE + SIGNATURE


def test_status_names_resolve_through_the_lookups(clock: Clock) -> None:
    ctx, sync = make(clock, {**SYNC_ON, "statuses": {"in_progress": "in progress", "resolved": 5}})
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    tid = task_of(sync, "ACME-1")
    sync.on_task_claimed(task_id=tid)
    sync.on_task_completed(task_id=tid, summary="ok")
    statuses = [u.get("status_id") for u in ctx.mcp.tool_calls("specivo_update_issue")]
    assert statuses == [2, 5]


def test_an_unknown_status_name_still_posts_the_comment(clock: Clock) -> None:
    ctx, sync = make(clock, {**SYNC_ON, "statuses": {"feedback": "Waiting"}})
    serve(ctx, [("ACME-1", "New", "One")])
    tick(sync, clock)
    sync.on_task_blocked(task_id=task_of(sync, "ACME-1"), reason="Need access")
    assert ctx.mcp.tool_calls("specivo_update_issue") == [
        {"issue_ref": "ACME-1", "notes": "Blocked in Hermes: Need access" + SIGNATURE}
    ]


def test_a_failed_card_creation_is_tried_again(clock: Clock) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "One")])
    ctx.board.fail.add("kanban_create")
    tick(sync, clock)
    assert sync.snapshot()["cards"] == {}
    ctx.board.fail.clear()
    tick(sync, clock)
    assert sync.snapshot()["cards"]["ACME-1"]["state"] == "open"


def test_listing_errors_do_not_raise(clock: Clock) -> None:
    ctx, sync = make(clock)
    ctx.mcp.handlers["specivo_list_issues"] = RuntimeError("Unknown assignee 'me' in ACME")
    tick(sync, clock)
    assert ctx.board.calls == []


@pytest.mark.parametrize("subject_len", [10, 400])
def test_long_subjects_are_clipped(clock: Clock, subject_len: int) -> None:
    ctx, sync = make(clock)
    serve(ctx, [("ACME-1", "New", "x" * subject_len)])
    tick(sync, clock)
    assert len(ctx.board.created()[0]["title"]) <= 200

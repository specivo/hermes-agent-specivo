from __future__ import annotations

import io
import subprocess
from typing import Any

import pytest
from conftest import BASE_URL, Clock, FakeCtx

from hermes_specivo.config import (
    DEFAULT_USAGE_COMMAND,
    USAGE_TIMEOUT_SECONDS,
    ConfigError,
    load_settings,
)
from hermes_specivo.runtime import Runtime
from hermes_specivo.setup_cmd import Io, run_status
from hermes_specivo.usage_report import USAGE_STATE_KEY, UsageReporter, describe_last_run

REPORTER = "/opt/tools/ai-usage"


class FakeProcess:
    def __init__(self, code: int | None = 0, hang: bool = False) -> None:
        self.code = code
        self.hang = hang
        self.killed = False

    def wait(self, timeout: float | None = None) -> int | None:
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ai-usage", timeout or 0)
        return -9 if self.killed else self.code

    def kill(self) -> None:
        self.killed = True


class Spawner:
    def __init__(self, process: FakeProcess | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.process = process or FakeProcess()
        self.error = error

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        if self.error is not None:
            raise self.error
        self.calls.append((argv, kwargs))
        return self.process


def _ctx(**usage: Any) -> FakeCtx:
    return FakeCtx({"url": BASE_URL, "usage_report": {"enabled": True, **usage}})


def _reporter(
    ctx: FakeCtx, clock: Clock, spawner: Spawner, *, found: bool = True, run_now: bool = True
) -> tuple[UsageReporter, list[Any]]:
    pending: list[Any] = []

    def background(target: Any) -> None:
        if run_now:
            target()
        else:
            pending.append(target)

    reporter = UsageReporter(
        Runtime(ctx, clock=clock),
        spawn=spawner,
        which=lambda name: REPORTER if found and name == "ai-usage" else None,
        background=background,
    )
    return reporter, pending


def test_off_by_default_and_settings_are_checked() -> None:
    settings = load_settings(lambda key, default: {"url": BASE_URL}.get(key, default))
    assert not settings.usage_report.enabled
    assert settings.usage_report.command == DEFAULT_USAGE_COMMAND
    custom = load_settings(
        lambda key, default: {"usage_report": {"command": "/opt/x/ai-usage push --source hermes"}}.get(key, default)
    )
    assert custom.usage_report.command == ("/opt/x/ai-usage", "push", "--source", "hermes")
    for bad, message in (
        ({"command": ""}, "must name the reporter"),
        ({"command": 5}, "command line or a list"),
        ({"min_interval_seconds": 10}, "between 60 and 86400"),
        ({"cron": True}, "unknown keys"),
    ):
        values: dict[str, Any] = {"usage_report": bad}
        with pytest.raises(ConfigError, match=message):
            load_settings(values.get)


def test_a_session_end_starts_the_reporter_with_the_plugin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_test_key")
    ctx, clock, spawner = _ctx(), Clock(), Spawner()
    reporter, _ = _reporter(ctx, clock, spawner)
    reporter.on_session_end(session_id="s1")
    [(argv, kwargs)] = spawner.calls
    assert argv == [REPORTER, "push", "--source", "hermes"]
    assert kwargs["env"]["SPECIVO_API_KEY"] == "spv_test_key"
    assert kwargs["start_new_session"] and kwargs["stdout"] is subprocess.DEVNULL
    state = ctx.state.get(USAGE_STATE_KEY)
    assert state["exit_code"] == 0 and state["finished"] == clock.now
    assert describe_last_run(state) == "last run sent everything"


def test_runs_are_spaced_and_never_overlap() -> None:
    ctx, clock, spawner = _ctx(min_interval_seconds=300), Clock(), Spawner()
    reporter, pending = _reporter(ctx, clock, spawner, run_now=False)
    reporter.on_session_end()
    clock.now += 400
    reporter.on_session_end()  # the first run has not finished yet
    assert len(spawner.calls) == 1
    pending.pop()()  # it finishes
    clock.now += 100
    reporter.on_session_end()  # 500 s after the start: due
    assert len(spawner.calls) == 2
    pending.pop()()
    clock.now += 60
    reporter.on_session_end()  # too soon
    assert len(spawner.calls) == 2


def test_a_run_that_hangs_is_stopped() -> None:
    ctx, clock, spawner = _ctx(), Clock(), Spawner(FakeProcess(hang=True))
    reporter, _ = _reporter(ctx, clock, spawner)
    reporter.on_session_end()
    assert spawner.process.killed
    state = ctx.state.get(USAGE_STATE_KEY)
    assert state["exit_code"] == "timeout"
    assert describe_last_run(state) == f"last run was stopped after {USAGE_TIMEOUT_SECONDS}s"


def test_a_missing_reporter_is_recorded_not_raised() -> None:
    ctx, clock, spawner = _ctx(), Clock(), Spawner()
    reporter, _ = _reporter(ctx, clock, spawner, found=False)
    reporter.on_session_end()
    assert spawner.calls == []
    assert "not found" in describe_last_run(ctx.state.get(USAGE_STATE_KEY))

    failing = Spawner(error=OSError("exec format error"))
    ctx2 = _ctx()
    reporter2, _ = _reporter(ctx2, Clock(), failing)
    reporter2.on_session_end()  # does not raise
    assert "could not start" in describe_last_run(ctx2.state.get(USAGE_STATE_KEY))


def test_disabled_or_not_set_up_does_nothing() -> None:
    spawner = Spawner()
    for ctx in (FakeCtx({"url": BASE_URL}), FakeCtx({"usage_report": {"enabled": True}})):
        reporter, _ = _reporter(ctx, Clock(), spawner)
        reporter.on_session_end()
    assert spawner.calls == []


def test_status_shows_the_last_run() -> None:
    ctx, clock, spawner = _ctx(), Clock(), Spawner(FakeProcess(code=1))
    reporter, _ = _reporter(ctx, clock, spawner)
    reporter.on_session_end()
    out = io.StringIO()
    assert run_status(Runtime(ctx, clock=clock), Io(out=out)) == 0
    text = out.getvalue()
    assert "Usage reporting: on" in text and "left records waiting" in text

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import BASE_URL, Clock, FakeCtx

from hermes_specivo.config import ConfigError
from hermes_specivo.runtime import Runtime
from hermes_specivo.setup_cmd import Io, SetupInput, YamlConfigStore, doctor_checks, merge, run_doctor
from hermes_specivo.webhook import (
    EVENTS,
    find_entry,
    normalize_receiver_url,
    outbound_entry,
    ping_receiver,
)

RECEIVER = f"{BASE_URL}/api/v1/hermes/events/tok_abc123/"


class FakeHttp:
    def __init__(self, status: int = 200, body: object = None, error: Exception | None = None) -> None:
        self.status = status
        self.body = json.dumps({"ok": True, "receiver": "laptop"} if body is None else body).encode()
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.status, self.body


def test_receiver_url_gets_its_trailing_slash() -> None:
    assert normalize_receiver_url(RECEIVER[:-1], BASE_URL) == RECEIVER
    assert normalize_receiver_url(RECEIVER) == RECEIVER
    prefixed = "https://specivo.example.test/tracker/api/v1/hermes/events/tok/"
    assert normalize_receiver_url(prefixed, "https://specivo.example.test/tracker") == prefixed


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("http://specivo.example.test/api/v1/hermes/events/tok/", "must use https"),
        (f"{BASE_URL}/api/v1/hermes/receivers/tok/", "must look like"),
        (f"{BASE_URL}/api/v1/hermes/events/", "must look like"),
        ("https://other.example.test/api/v1/hermes/events/tok/", "must be on the Specivo server"),
    ],
)
def test_bad_receiver_urls(raw: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        normalize_receiver_url(raw, BASE_URL)


def test_outbound_entry_keeps_the_secret_out() -> None:
    entry = outbound_entry(RECEIVER)
    assert entry == {
        "name": "specivo",
        "url": RECEIVER,
        "events": list(EVENTS),
        "secret_env": "SPECIVO_WEBHOOK_SECRET",
        "timeout": 10,
    }
    assert find_entry({"hooks": {"outbound": [{"name": "x", "url": "https://a.example.test"}, entry]}}) == entry
    unnamed = {"url": RECEIVER, "events": ["post_tool_call"]}
    assert find_entry({"hooks": {"outbound": [unnamed]}}) == unnamed
    assert find_entry({"hooks": {"outbound": "broken"}}) is None


@pytest.mark.parametrize(
    ("http", "ok", "detail"),
    [
        (FakeHttp(), True, "receiver 'laptop' is active"),
        (FakeHttp(status=404), False, "does not exist or is switched off"),
        (FakeHttp(status=500), False, "HTTP 500"),
        (FakeHttp(body=["not", "a", "dict"]), False, "unexpected answer"),
        (FakeHttp(error=OSError("connection refused")), False, "could not reach"),
    ],
)
def test_ping(http: FakeHttp, ok: bool, detail: str) -> None:
    ping = ping_receiver(RECEIVER, http)
    assert ping.ok is ok and detail in ping.detail
    assert http.urls == [RECEIVER]


def configured(tmp_path: Path, receiver: str | None = RECEIVER) -> YamlConfigStore:
    store = YamlConfigStore(tmp_path / "config.yaml")
    doc, written = merge({}, SetupInput(url=BASE_URL, receiver_url=receiver))
    store.write(doc, written)
    return store


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_test")
    monkeypatch.setenv("SPECIVO_WEBHOOK_SECRET", "whsec_test")


@pytest.mark.usefixtures("secrets")
def test_doctor_all_good(tmp_path: Path, runtime: Runtime, ctx: FakeCtx) -> None:
    http = FakeHttp()
    checks = doctor_checks(runtime, configured(tmp_path), http)
    assert [c.level for c in checks] == ["ok", "ok", "ok", "ok"]
    assert "signed in to Specivo as agent-bot" in checks[2].message
    assert ctx.mcp.tool_calls("specivo_whoami") == [{}]
    out = io.StringIO()
    assert run_doctor(runtime, configured(tmp_path), Io(out=out), http) == 0
    assert out.getvalue().endswith("All required checks passed.\n")


def test_doctor_reports_what_is_missing(tmp_path: Path, runtime: Runtime, ctx: FakeCtx) -> None:
    ctx.mcp.allowlist = set()
    store = YamlConfigStore(tmp_path / "config.yaml")
    store.write({"hooks": {"outbound": [{"name": "specivo", "url": RECEIVER, "events": ["post_tool_call"]}]}}, set())
    checks = doctor_checks(runtime, store, FakeHttp(status=404))
    failures = [c.message for c in checks if c.level == "fail"]
    assert any("mcp_servers.specivo is missing" in m for m in failures)
    assert any("mcp_allowlist lacks 'specivo'" in m for m in failures)
    assert any("SPECIVO_API_KEY is not set" in m for m in failures)
    assert any("must use secret_env: SPECIVO_WEBHOOK_SECRET" in m for m in failures)
    assert any("switched off" in m for m in failures)
    assert any("does not send" in c.message for c in checks if c.level == "warn")
    out = io.StringIO()
    assert run_doctor(runtime, store, Io(out=out), FakeHttp(status=404)) == 1


@pytest.mark.usefixtures("secrets")
def test_doctor_reports_mcp_failure_and_missing_webhook(tmp_path: Path, runtime: Runtime, ctx: FakeCtx) -> None:
    ctx.mcp.handlers["specivo_whoami"] = RuntimeError("401 Unauthorized")
    checks = doctor_checks(runtime, configured(tmp_path, receiver=None), FakeHttp())
    assert any(c.level == "fail" and "401 Unauthorized" in c.message for c in checks)
    assert any(c.level == "warn" and "--receiver-url" in c.message for c in checks)


@pytest.mark.usefixtures("secrets")
def test_doctor_warns_about_an_untrusted_server_and_wrong_url(tmp_path: Path, runtime: Runtime) -> None:
    store = configured(tmp_path)
    doc = store.read()
    doc["mcp_servers"]["specivo"]["trust"] = "untrusted"
    doc["mcp_servers"]["specivo"]["url"] = "https://elsewhere.example.test/mcp/"
    store.write(doc, set())
    messages = [(c.level, c.message) for c in doctor_checks(runtime, store, FakeHttp())]
    assert any(level == "warn" and "untrusted" in m for level, m in messages)
    assert any(level == "fail" and "expected https://specivo.example.test/mcp/" in m for level, m in messages)


def test_doctor_before_setup_and_with_bad_settings(tmp_path: Path, clock: Clock) -> None:
    store = YamlConfigStore(tmp_path / "config.yaml")
    checks = doctor_checks(Runtime(FakeCtx({}), clock=clock), store)
    assert [(c.level, "hermes specivo setup" in c.message) for c in checks] == [("fail", True)]
    bad = doctor_checks(Runtime(FakeCtx({"issue_sync": {"poll_seconds": 1}}), clock=clock), store)
    assert bad[0].level == "fail" and "poll_seconds must be at least 15" in bad[0].message


@pytest.mark.usefixtures("secrets")
def test_doctor_skips_the_webhook_when_events_are_off(tmp_path: Path, clock: Clock) -> None:
    runtime = Runtime(FakeCtx({"url": BASE_URL, "events": {"enabled": False}}), clock=clock)
    http = FakeHttp()
    checks = doctor_checks(runtime, configured(tmp_path, receiver=None), http)
    assert all(c.level == "ok" for c in checks)
    assert http.urls == []


@pytest.mark.usefixtures("secrets")
def test_doctor_connects_the_mcp_server_before_whoami(tmp_path: Path, runtime: Runtime, ctx: FakeCtx) -> None:
    order: list[str] = []
    original = ctx.call_mcp

    def call_mcp(*args: object, **kwargs: object) -> dict[str, object]:
        order.append("call")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    ctx.call_mcp = call_mcp  # type: ignore[method-assign]
    checks = doctor_checks(runtime, configured(tmp_path), FakeHttp(), connect_mcp=lambda name: order.append(name))
    assert order[:2] == ["specivo", "call"]
    assert [c.level for c in checks] == ["ok", "ok", "ok", "ok"]


@pytest.mark.usefixtures("secrets")
def test_doctor_reports_a_failed_connect_and_still_tries_whoami(tmp_path: Path, runtime: Runtime, ctx: FakeCtx) -> None:
    def broken(name: str) -> None:
        raise RuntimeError("no MCP SDK")

    checks = doctor_checks(runtime, configured(tmp_path), FakeHttp(), connect_mcp=broken)
    assert ("warn", "could not connect MCP server 'specivo': no MCP SDK") in [(c.level, c.message) for c in checks]
    assert ctx.mcp.tool_calls("specivo_whoami") == [{}]

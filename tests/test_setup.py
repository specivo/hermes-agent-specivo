from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import BASE_URL, Clock, FakeCtx

from hermes_specivo.runtime import Runtime
from hermes_specivo.setup_cmd import (
    Io,
    SetupInput,
    YamlConfigStore,
    add_arguments,
    describe_changes,
    merge,
    run_setup,
    run_status,
)

RECEIVER = f"{BASE_URL}/api/v1/hermes/events/tok_abc123/"

#: A user's config.yaml with settings of their own in and around the blocks setup touches.
EXISTING = """\
model:
  default: some-model
mcp_servers:
  notes:
    url: https://notes.example.test/mcp
  specivo:
    trust: full
    tools:
      exclude: [specivo_list_projects]
plugins:
  entries:
    specivo:
      enabled: true
      mcp_allowlist: [notes]
      settings:
        issue_sync:
          enabled: true
          project: OPS
hooks:
  outbound:
    - name: audit
      url: https://audit.example.test/hook
      events: [on_session_end]
"""

GOLDEN = """\
model:
  default: some-model
mcp_servers:
  notes:
    url: https://notes.example.test/mcp
  specivo:
    trust: full
    tools:
      exclude:
      - specivo_list_projects
      - specivo_delete_*
    url: https://specivo.example.test/mcp/
    headers:
      Authorization: Bearer ${SPECIVO_API_KEY}
    timeout: 120
plugins:
  entries:
    specivo:
      enabled: true
      mcp_allowlist:
      - notes
      - specivo
      settings:
        issue_sync:
          enabled: true
          project: OPS
        url: https://specivo.example.test
        default_project: ACME
hooks:
  outbound:
  - name: audit
    url: https://audit.example.test/hook
    events:
    - on_session_end
  - name: specivo
    url: https://specivo.example.test/api/v1/hermes/events/tok_abc123/
    events:
    - on_session_start
    - post_tool_call
    - pre_approval_request
    - post_approval_response
    - subagent_start
    - subagent_stop
    - on_session_end
    secret_env: SPECIVO_WEBHOOK_SECRET
    timeout: 10
"""


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    return parser.parse_args(argv)


class Term:
    def __init__(self, interactive: bool = False, answer: str = "y") -> None:
        self.out = io.StringIO()
        self.questions: list[str] = []
        self.io = Io(out=self.out, interactive=lambda: interactive, ask=self._ask)
        self.answer = answer

    def _ask(self, question: str) -> str:
        self.questions.append(question)
        return self.answer

    @property
    def text(self) -> str:
        return self.out.getvalue()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary HERMES_HOME holding the user's config.yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(EXISTING, encoding="utf-8")
    return tmp_path


def setup(home: Path, clock: Clock, *argv: str, term: Term | None = None, ctx: FakeCtx | None = None) -> int:
    term = term or Term()
    runtime = Runtime(ctx or FakeCtx({}), clock=clock)
    return run_setup(runtime, parse("setup", *argv), YamlConfigStore(home / "config.yaml"), term.io, clock)


def backups(home: Path) -> list[Path]:
    return sorted(home.glob("config.yaml.specivo-*.bak"))


def test_setup_writes_the_golden_config_without_clobbering(home: Path, clock: Clock) -> None:
    term = Term()
    code = setup(
        home, clock, "--url", BASE_URL + "/", "--receiver-url", RECEIVER[:-1], "--project", "acme", "--yes", term=term
    )
    assert code == 0, term.text
    assert (home / "config.yaml").read_text(encoding="utf-8") == GOLDEN
    assert [p.read_text(encoding="utf-8") for p in backups(home)] == [EXISTING]
    assert "Written. Backup:" in term.text
    assert "SPECIVO_API_KEY is not set" in term.text
    assert "SPECIVO_WEBHOOK_SECRET is not set" in term.text


def test_setup_twice_changes_nothing_and_makes_no_second_backup(home: Path, clock: Clock) -> None:
    args = ("--url", BASE_URL, "--receiver-url", RECEIVER, "--project", "ACME", "--yes")
    assert setup(home, clock, *args) == 0
    first = (home / "config.yaml").read_text(encoding="utf-8")
    clock.now += 3600
    term = Term()
    assert setup(home, clock, *args, term=term) == 0
    assert (home / "config.yaml").read_text(encoding="utf-8") == first
    assert len(backups(home)) == 1
    assert "nothing to change" in term.text
    assert "Restart Hermes" not in term.text


def test_setup_updates_a_changed_receiver_in_place(home: Path, clock: Clock) -> None:
    setup(home, clock, "--url", BASE_URL, "--receiver-url", RECEIVER, "--yes")
    doc = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    doc["hooks"]["outbound"][1]["timeout"] = 30  # the user's own tweak survives
    (home / "config.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    clock.now += 5
    new_receiver = f"{BASE_URL}/api/v1/hermes/events/tok_new999/"
    setup(home, clock, "--url", BASE_URL, "--receiver-url", new_receiver, "--yes")
    outbound = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["hooks"]["outbound"]
    assert [e["name"] for e in outbound] == ["audit", "specivo"]
    assert outbound[1]["url"] == new_receiver and outbound[1]["timeout"] == 30


def test_setup_into_an_empty_home(tmp_path: Path, clock: Clock) -> None:
    term = Term()
    assert setup(tmp_path, clock, "--url", "http://localhost:8030", "--yes", term=term) == 0
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert doc == {
        "mcp_servers": {
            "specivo": {
                "url": "http://localhost:8030/mcp/",
                "headers": {"Authorization": "Bearer ${SPECIVO_API_KEY}"},
                "timeout": 120,
                "tools": {"exclude": ["specivo_delete_*"]},
            }
        },
        "plugins": {
            "entries": {"specivo": {"mcp_allowlist": ["specivo"], "settings": {"url": "http://localhost:8030"}}}
        },
    }
    assert backups(tmp_path) == []
    assert term.text.endswith(
        "Written.\nwarning: SPECIVO_API_KEY is not set. Add it with `hermes config set "
        "SPECIVO_API_KEY ...` (it is never printed).\nRestart Hermes (and the gateway, if you run "
        "one) so the MCP server and webhook are loaded.\nThen run `hermes specivo doctor`.\n"
    )


def test_setup_asks_before_writing(home: Path, clock: Clock) -> None:
    term = Term(interactive=True, answer="n")
    assert setup(home, clock, "--url", BASE_URL, term=term) == 1
    assert (home / "config.yaml").read_text(encoding="utf-8") == EXISTING
    assert term.questions and "Changes to" in term.text and "+ mcp_servers.specivo.url" in term.text
    term = Term(interactive=True, answer="yes")
    assert setup(home, clock, "--url", BASE_URL, term=term) == 0
    assert "example.test/mcp/" in (home / "config.yaml").read_text(encoding="utf-8")


def test_setup_without_a_terminal_needs_yes(home: Path, clock: Clock) -> None:
    term = Term(interactive=False)
    assert setup(home, clock, "--url", BASE_URL, term=term) == 2
    assert "Re-run with --yes" in term.text
    assert (home / "config.yaml").read_text(encoding="utf-8") == EXISTING


def test_print_only_shows_the_blocks(home: Path, clock: Clock) -> None:
    term = Term()
    assert setup(home, clock, "--url", BASE_URL, "--receiver-url", RECEIVER, "--print", term=term) == 0
    assert (home / "config.yaml").read_text(encoding="utf-8") == EXISTING
    printed = yaml.safe_load(term.text)
    assert set(printed) == {"mcp_servers", "plugins", "hooks"}
    assert list(printed["mcp_servers"]) == ["specivo"]
    assert printed["hooks"]["outbound"][0]["secret_env"] == "SPECIVO_WEBHOOK_SECRET"


def test_secrets_are_never_written_or_printed(home: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_supersecretvalue")
    monkeypatch.setenv("SPECIVO_WEBHOOK_SECRET", "whsec_supersecretvalue")
    term = Term()
    setup(home, clock, "--url", BASE_URL, "--receiver-url", RECEIVER, "--yes", term=term)
    written = (home / "config.yaml").read_text(encoding="utf-8")
    for secret in ("spv_supersecretvalue", "whsec_supersecretvalue"):
        assert secret not in written and secret not in term.text
    assert "is not set" not in term.text


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ((), "give the Specivo address"),
        (("--url", "http://specivo.example.test"), "must use https"),
        (("--url", BASE_URL, "--receiver-url", f"{BASE_URL}/hooks/x"), "receiver URL must look like"),
        (
            ("--url", BASE_URL, "--receiver-url", "https://other.example.test/api/v1/hermes/events/t/"),
            "must be on the Specivo server",
        ),
    ],
)
def test_setup_rejects_bad_input(home: Path, clock: Clock, argv: tuple[str, ...], message: str) -> None:
    term = Term()
    assert setup(home, clock, *argv, "--yes", term=term) == 2
    assert message in term.text
    assert (home / "config.yaml").read_text(encoding="utf-8") == EXISTING


def test_setup_reuses_the_configured_url(home: Path, clock: Clock) -> None:
    ctx = FakeCtx({"url": BASE_URL})
    assert setup(home, clock, "--project", "ACME", "--yes", ctx=ctx) == 0
    assert f"url: {BASE_URL}/mcp/" in (home / "config.yaml").read_text(encoding="utf-8")


def test_options_server_name_and_delete_tools() -> None:
    spec = SetupInput(url=BASE_URL, server="tracker", exclude_destructive=False)
    doc, written = merge({}, spec)
    assert "tools" not in doc["mcp_servers"]["tracker"]
    assert doc["plugins"]["entries"]["specivo"]["settings"]["mcp_server"] == "tracker"
    assert doc["plugins"]["entries"]["specivo"]["mcp_allowlist"] == ["tracker"]
    assert ("plugins", "entries", "specivo", "settings", "mcp_server") in written


def test_merge_does_not_mutate_its_input() -> None:
    before: dict[str, Any] = yaml.safe_load(EXISTING)
    snapshot = yaml.safe_dump(before)
    merge(before, SetupInput(url=BASE_URL, receiver_url=RECEIVER))
    assert yaml.safe_dump(before) == snapshot


def test_describe_changes() -> None:
    before = {"a": {"b": 1, "c": 2}}
    after = {"a": {"b": 1, "c": 3, "d": [1]}}
    assert describe_changes(before, after) == ["  ~ a.c: 2 -> 3", "  + a.d: [1]"]
    assert describe_changes(after, after) == []


def test_status_summarises_state(runtime: Runtime, ctx: FakeCtx) -> None:
    ctx.settings["issue_sync"] = {"enabled": True}
    ctx.state.set("sessions", {"s1": {"issue": "ACME-1"}, "s2": {}})
    ctx.state.set("kanban", {"last_poll": 0, "cards": {"ACME-1": {"state": "open"}, "ACME-2": {"state": "claimed"}}})
    term = Term()
    assert run_status(runtime, term.io) == 0
    assert "Sessions seen: 2 (1 with a current issue)" in term.text
    assert "cards: 1 claimed, 1 open" in term.text
    assert "last poll: never" in term.text

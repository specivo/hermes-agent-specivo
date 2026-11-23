"""``register(ctx)`` through the plugin's real entry point, loaded the way Hermes loads a directory plugin."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from conftest import BASE_URL, LOOKUPS, WHOAMI, Clock, FakeCtx

from hermes_specivo.mcp import SpecivoClient, SpecivoError, parse_issue_list, parse_statuses, result_text
from hermes_specivo.plugin import HOOKS, context_section, register, register_skills
from hermes_specivo.runtime import Runtime

ROOT = Path(__file__).resolve().parent.parent


def load_entry_point() -> ModuleType:
    """Import the repository root as a package, as Hermes does with a plugin directory."""
    name = "hermes_plugin_specivo_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_entry_point_registers_exactly_the_declared_hooks() -> None:
    ctx = FakeCtx({"url": BASE_URL})
    load_entry_point().register(ctx)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"]) == sorted(HOOKS)
    assert manifest["provides_tools"] == []
    assert [e["name"] for e in manifest["requires_env"]] == ["SPECIVO_API_KEY", "SPECIVO_WEBHOOK_SECRET"]
    assert manifest["python_dependencies"] == []
    assert set(ctx.commands) == {"specivo"} and set(ctx.cli_commands) == {"specivo"}
    assert set(ctx.prompt_sections) == {"specivo.context"}


def test_manifest_version_matches_the_package() -> None:
    from hermes_specivo import __version__

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["name"] == "specivo"


def test_hooks_accept_extra_keywords() -> None:
    """Hermes adds payload fields over time; every callback takes **kwargs."""
    ctx = FakeCtx({"url": BASE_URL})
    register(ctx)
    for callbacks in ctx.hooks.values():
        for callback in callbacks:
            callback(future_field="x", session_id="s1", task_id="", board=None)  # must not raise


def test_cli_command_parses(tmp_path: Path) -> None:
    ctx = FakeCtx({"url": BASE_URL})
    register(ctx)
    entry = ctx.cli_commands["specivo"]
    parser = argparse.ArgumentParser()
    entry["setup_fn"](parser)
    args = parser.parse_args(["setup", "--url", BASE_URL, "--print"])
    assert args.specivo_command == "setup" and args.print is True
    assert callable(entry["handler_fn"])


def test_context_section(clock: Clock) -> None:
    runtime = Runtime(FakeCtx({"url": BASE_URL, "default_project": "ACME"}), clock=clock)
    text = context_section({}, runtime=runtime)
    assert BASE_URL in text and "ACME" in text and "mcp__specivo__specivo_*" in text
    assert len(text) < 2000
    assert context_section({}, runtime=Runtime(FakeCtx({}), clock=clock)) == ""
    off = Runtime(FakeCtx({"url": BASE_URL, "context": {"inject": False}}), clock=clock)
    assert context_section({}, runtime=off) == ""


def test_skills_are_registered_from_their_folders(tmp_path: Path) -> None:
    (tmp_path / "specivo-workflow").mkdir()
    (tmp_path / "specivo-workflow" / "SKILL.md").write_text("---\nname: specivo-workflow\n---\n", encoding="utf-8")
    (tmp_path / "notes").mkdir()  # no SKILL.md: not a skill
    ctx = FakeCtx()
    assert register_skills(ctx, tmp_path) == ["specivo-workflow"]
    assert register_skills(ctx, tmp_path / "missing") == []


def test_result_text_unwraps_hermes_envelopes() -> None:
    assert result_text({"ok": True, "result": "plain"}) == "plain"
    assert result_text({"ok": True, "result": json.dumps({"result": "wrapped"})}) == "wrapped"
    assert result_text({"ok": True, "result": [{"type": "text", "text": "a"}, {"text": "b"}]}) == "a\nb"
    with pytest.raises(SpecivoError, match="boom"):
        result_text({"ok": False, "error": "boom"})
    with pytest.raises(SpecivoError, match="nope"):
        result_text({"ok": True, "result": {"error": "nope"}})


def test_parsers() -> None:
    page = parse_issue_list(
        "Issues for ACME (3 total, filter=open):\n\n"
        "  ACME-1  [New]  First  [tags: ui, bug]\n"
        "  ACME-2  [In Progress]  Second [with] brackets\n"
    )
    assert page.total == 3
    assert [(r.key, r.status, r.subject) for r in page.rows] == [
        ("ACME-1", "New", "First"),
        ("ACME-2", "In Progress", "Second [with] brackets"),
    ]
    assert parse_issue_list("Issues for ACME (0 total, filter=open):\n\n  (none)").rows == []
    statuses = parse_statuses(LOOKUPS)
    assert [(s.id, s.name, s.is_finished) for s in statuses] == [
        (1, "New", False),
        (2, "In Progress", False),
        (3, "Resolved", True),
        (4, "Feedback", False),
        (5, "Closed", True),
    ]


def test_client_turns_failures_into_specivo_errors() -> None:
    ctx = FakeCtx()
    client = SpecivoClient(ctx, "specivo")
    assert client.whoami() == WHOAMI
    ctx.mcp.allowlist = set()
    with pytest.raises(SpecivoError, match="mcp_allowlist"):
        client.whoami()

    class Exploding:
        def call_mcp(self, *a: object, **k: object) -> dict[str, object]:
            raise TimeoutError("timed out")

    with pytest.raises(SpecivoError, match="timed out"):
        SpecivoClient(Exploding(), "specivo").whoami()

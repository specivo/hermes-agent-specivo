"""``memory`` settings: defaults, validation and which project a session resolves to."""

from __future__ import annotations

from typing import Any

import pytest

from hermes_specivo.config import ConfigError, Settings, load_settings
from hermes_specivo.memory.scope import Scope, resolve_scope


def settings(raw: dict[str, Any]) -> Settings:
    return load_settings(lambda key, default: raw.get(key, default))


def test_defaults_without_a_memory_block() -> None:
    memory = settings({}).memory
    assert memory.project is None and memory.notes_page == "Agent Notes"
    assert (memory.max_hits, memory.max_chars, memory.mirror_builtin) == (5, 1800, False)
    assert dict(memory.cwd_projects) == {} and dict(memory.chat_projects) == {}


def test_full_memory_block() -> None:
    memory = settings(
        {
            "memory": {
                "project": "ACME",
                "cwd_projects": {"/srv/work/acme": "ACME", "/srv/work/beta": "BETA"},
                "chat_projects": {"telegram:12345": "OPS"},
                "notes_page": "Team Notes",
                "max_hits": 3,
                "max_chars": 900,
                "mirror_builtin": True,
            }
        }
    ).memory
    assert memory.project == "ACME" and memory.cwd_projects["/srv/work/beta"] == "BETA"
    assert memory.chat_projects == {"telegram:12345": "OPS"}
    assert (memory.notes_page, memory.max_hits, memory.max_chars, memory.mirror_builtin) == ("Team Notes", 3, 900, True)


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        ({"projct": "ACME"}, "unknown keys"),
        ({"project": "acme"}, "memory.project"),
        ({"cwd_projects": {"/srv/a": "lower"}}, "memory.cwd_projects./srv/a"),
        ({"cwd_projects": ["/srv/a"]}, "must be a mapping"),
        ({"chat_projects": {"telegram:1": None}}, "must name a project key"),
        ({"max_hits": 0}, "between 1 and 10"),
        ({"max_hits": 11}, "between 1 and 10"),
        ({"max_hits": True}, "whole number"),
        ({"max_chars": 50}, "between 200 and 8000"),
        ({"max_chars": "900"}, "whole number"),
        ({"notes_page": "---"}, "notes_page"),
        ({"notes_page": "x" * 256}, "notes_page"),
        ({"mirror_builtin": "yes"}, "true or false"),
    ],
)
def test_invalid_memory_settings_name_the_key(memory: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace(".", r"\.")):
        settings({"memory": memory})


def test_memory_must_be_a_mapping() -> None:
    with pytest.raises(ConfigError, match="memory must be a mapping"):
        settings({"memory": "ACME"})


def scope(raw: dict[str, Any], **kwargs: str | None) -> Scope:
    values: dict[str, str | None] = {"cwd": None, "platform": None, "chat_id": None, **kwargs}
    return resolve_scope(settings(raw), cwd=values["cwd"], platform=values["platform"], chat_id=values["chat_id"])


MAPS = {
    "default_project": "DEF",
    "memory": {
        "cwd_projects": {"/srv/work": "WORK", "/srv/work/acme": "ACME"},
        "chat_projects": {"telegram:12345": "CHAT"},
    },
}


def test_project_precedence() -> None:
    explicit = {**MAPS, "memory": {**MAPS["memory"], "project": "PIN"}}  # type: ignore[dict-item]
    assert scope(explicit, cwd="/srv/work/acme") == Scope("PIN", "memory.project")
    assert scope(MAPS, cwd="/srv/work/acme/src", platform="telegram", chat_id="12345") == Scope(
        "ACME", "memory.cwd_projects"
    )
    assert scope(MAPS, cwd="/srv/work/other").project == "WORK"
    assert scope(MAPS, cwd="/srv/elsewhere", platform="telegram", chat_id="12345") == Scope(
        "CHAT", "memory.chat_projects"
    )
    assert scope(MAPS, platform="telegram", chat_id="999") == Scope("DEF", "default_project")
    assert scope({}) == Scope(None, None)


def test_cwd_prefix_matches_whole_path_segments() -> None:
    raw = {"memory": {"cwd_projects": {"/srv/work/acme": "ACME"}}}
    assert scope(raw, cwd="/srv/work/acme").project == "ACME"
    assert scope(raw, cwd="/srv/work/acme-old").project is None

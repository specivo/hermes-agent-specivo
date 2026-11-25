from __future__ import annotations

from typing import Any

import pytest

from hermes_specivo.config import (
    DEFAULT_STATUSES,
    ConfigError,
    get_secret,
    is_loopback_host,
    load_settings,
    normalize_base_url,
)


def settings_from(values: dict[str, Any]) -> Any:
    return load_settings(lambda key, default: values.get(key, default))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://specivo.example.test", "https://specivo.example.test"),
        ("https://specivo.example.test/", "https://specivo.example.test"),
        ("  https://Specivo.Example.Test:8443/tracker/ ", "https://specivo.example.test:8443/tracker"),
        ("http://localhost:8030", "http://localhost:8030"),
        ("http://127.0.0.1:8030/", "http://127.0.0.1:8030"),
        ("http://[::1]:8030", "http://[::1]:8030"),
    ],
)
def test_base_url_accepted(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "url is not set"),
        ("", "url is not set"),
        ("specivo.example.test", "http(s) URL with a host"),
        ("ftp://specivo.example.test", "http(s) URL with a host"),
        ("http://specivo.example.test", "must use https"),
        ("http://10.0.0.5:8030", "must use https"),
        ("https://user:pw@specivo.example.test", "user name or password"),
        ("https://specivo.example.test/?a=1", "query string"),
        ("https://specivo.example.test/mcp/", "drop the trailing '/mcp'"),
        ("https://specivo.example.test/api/v1", "drop the trailing '/api/v1'"),
    ],
)
def test_base_url_rejected_with_a_clear_message(raw: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        normalize_base_url(raw)


def test_loopback_detection() -> None:
    assert is_loopback_host("localhost")
    assert is_loopback_host("api.localhost")
    assert is_loopback_host("127.0.0.2")
    assert not is_loopback_host("specivo.example.test")
    assert not is_loopback_host("192.0.2.1")


def test_defaults_when_nothing_is_configured() -> None:
    settings = settings_from({})
    assert settings.url is None
    assert settings.mcp_server == "specivo"
    assert settings.context_inject is True
    assert settings.events_enabled is True
    assert settings.issue_sync.enabled is False
    assert settings.issue_sync.assignee == "me"
    assert settings.issue_sync.poll_seconds == 300
    assert dict(settings.issue_sync.statuses) == DEFAULT_STATUSES
    with pytest.raises(ConfigError, match="hermes specivo setup"):
        settings.require_url()


def test_full_settings() -> None:
    settings = settings_from(
        {
            "url": "https://specivo.example.test/",
            "mcp_server": "tracker",
            "default_project": "ACME",
            "context": {"inject": False},
            "events": {"enabled": False},
            "issue_sync": {
                "enabled": True,
                "board": "ops",
                "card_assignee": "coder",
                "select": {"tag": "agent"},
                "poll_seconds": 120,
                "statuses": {"in_progress": 2, "resolved": "Done", "feedback": "7"},
            },
        }
    )
    assert settings.url == "https://specivo.example.test"
    assert settings.mcp_server == "tracker"
    assert settings.context_inject is False
    assert settings.events_enabled is False
    sync = settings.issue_sync
    assert (sync.enabled, sync.board, sync.card_assignee, sync.assignee, sync.tag) == (
        True,
        "ops",
        "coder",
        "me",
        "agent",
    )
    assert dict(sync.statuses) == {"in_progress": 2, "resolved": "Done", "feedback": 7}
    assert settings.sync_project() == "ACME"
    assert settings.issue_link("ACME-3") == "https://specivo.example.test/issue/ACME-3/"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"url": "http://specivo.example.test"}, "must use https"),
        ({"mcp_server": "bad name"}, "mcp_server must be a simple name"),
        ({"default_project": "acme"}, "default_project must be a Specivo project key"),
        ({"context": "yes"}, "context must be a mapping"),
        ({"context": {"inject": "yes"}}, "context.inject must be true or false"),
        ({"issue_sync": {"poll_seconds": 5}}, "poll_seconds must be at least 15"),
        ({"issue_sync": {"poll_seconds": "60"}}, "poll_seconds must be a whole number"),
        ({"issue_sync": {"statuses": {"done": 3}}}, r"unknown keys \['done'\]"),
        ({"issue_sync": {"statuses": {"resolved": True}}}, "status name or a numeric status id"),
        ({"issue_sync": {"statuses": {"resolved": 0}}}, "positive status id"),
        ({"issue_sync": {"select": {"assignee": None}}}, "select.assignee must be 'me'"),
        ({"issue_sync": {"select": {"assignee": "someone-else"}}}, "select.assignee must be 'me'"),
        ({"issue_sync": {"enabled": True}}, "no project is set"),
    ],
)
def test_invalid_settings_name_the_key(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        settings_from(values)


def test_sync_project_falls_back_to_default_project() -> None:
    settings = settings_from({"default_project": "ACME", "issue_sync": {"enabled": True, "project": "OPS"}})
    assert settings.sync_project() == "OPS"


def test_secrets_come_from_the_environment_outside_hermes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_secret("SPECIVO_API_KEY") is None
    monkeypatch.setenv("SPECIVO_API_KEY", "")
    assert get_secret("SPECIVO_API_KEY") is None
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_test")
    assert get_secret("SPECIVO_API_KEY") == "spv_test"


def test_secrets_go_through_the_hermes_secret_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    scope = types.ModuleType("agent.secret_scope")
    seen: list[str] = []

    def fake_get_secret(name: str, default: str | None = None) -> str | None:
        seen.append(name)
        return "from-scope"

    scope.get_secret = fake_get_secret  # type: ignore[attr-defined]
    package = types.ModuleType("agent")
    package.secret_scope = scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("SPECIVO_API_KEY", "from-environ")
    assert get_secret("SPECIVO_API_KEY") == "from-scope"
    assert seen == ["SPECIVO_API_KEY"]

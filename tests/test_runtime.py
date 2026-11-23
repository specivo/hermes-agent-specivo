from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import Clock, FakeCtx

from hermes_specivo.runtime import REDACTION_UNAVAILABLE, Runtime


def test_redact_uses_hermes_at_full_strength(clock: Clock) -> None:
    ctx = FakeCtx()
    assert Runtime(ctx, clock=clock).redact("key sk-test-secret here") == "key «redacted» here"
    assert ctx.redacted == ["key sk-test-secret here"]


class NoRedactor:
    """A context without ``redact_text`` (an older Hermes)."""


class Raising(FakeCtx):
    def redact_text(self, text: str, *, force: bool = False) -> str:
        raise RuntimeError("redactor crashed")


class NoForce(FakeCtx):
    def redact_text(self, text: str) -> str:  # type: ignore[override]
        return text


class NotAString(FakeCtx):
    def redact_text(self, text: str, *, force: bool = False) -> Any:
        return None


@pytest.mark.parametrize("ctx", [NoRedactor(), Raising(), NoForce(), NotAString()])
def test_redact_fails_closed(ctx: Any, clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert Runtime(ctx, clock=clock).redact("key sk-test-secret here") == REDACTION_UNAVAILABLE
    assert "redaction" in caplog.text
    assert "sk-test-secret" not in caplog.text

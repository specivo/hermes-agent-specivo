"""The ``hooks.outbound`` entry that sends Hermes events to the Specivo event receiver.

Hermes signs every delivery with HMAC-SHA256 (``X-Hermes-Signature-256``) using the secret in
``SPECIVO_WEBHOOK_SECRET``; the plugin only writes the entry and checks the URL answers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from .config import WEBHOOK_SECRET_ENV, ConfigError, normalize_base_url

ENTRY_NAME = "specivo"
#: The events the receiver maps (the same list Specivo's ready-made block subscribes to).
EVENTS: tuple[str, ...] = (
    "on_session_start",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    "subagent_start",
    "subagent_stop",
    "on_session_end",
)
TIMEOUT_SECONDS = 10
RECEIVER_PATH_RE = re.compile(r"/api/v1/hermes/events/([A-Za-z0-9_-]+)/?$")

#: ``(url, timeout) -> (status code, body)``; replaced in tests so nothing touches the network.
HttpGet = Callable[[str, float], tuple[int, bytes]]


def normalize_receiver_url(raw: object, base_url: str | None = None) -> str:
    """The receiver URL with its required trailing slash, or :class:`ConfigError`.

    It must be ``<specivo>/api/v1/hermes/events/<token>/`` on the same server as *base_url*
    (when given), under the same https-or-loopback rule as the server URL.
    """
    url = normalize_base_url(raw, key="receiver URL")
    parts = urlsplit(url)
    if not RECEIVER_PATH_RE.search(parts.path):
        raise ConfigError(
            "receiver URL must look like https://your-specivo-host/api/v1/hermes/events/<token>/ "
            "(copy it from the receiver you created in Specivo)"
        )
    if base_url is not None:
        base = urlsplit(base_url)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            raise ConfigError(f"receiver URL must be on the Specivo server {base.scheme}://{base.netloc}, got {url!r}")
    return url if url.endswith("/") else url + "/"


def outbound_entry(receiver_url: str) -> dict[str, Any]:
    """The ``hooks.outbound`` list item for the receiver. The secret stays in the environment."""
    return {
        "name": ENTRY_NAME,
        "url": receiver_url,
        "events": list(EVENTS),
        "secret_env": WEBHOOK_SECRET_ENV,
        "timeout": TIMEOUT_SECONDS,
    }


def is_specivo_entry(entry: object) -> bool:
    """Whether a ``hooks.outbound`` item is ours: named ``specivo`` or pointing at a Specivo receiver."""
    if not isinstance(entry, dict):
        return False
    url = entry.get("url")
    return entry.get("name") == ENTRY_NAME or (isinstance(url, str) and bool(RECEIVER_PATH_RE.search(url)))


def find_entry(config: dict[str, Any]) -> dict[str, Any] | None:
    hooks = config.get("hooks")
    outbound = hooks.get("outbound") if isinstance(hooks, dict) else None
    if not isinstance(outbound, list):
        return None
    return next((e for e in outbound if is_specivo_entry(e)), None)


def default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https or loopback only
            return int(response.status), response.read(4096)
    except urlerror.HTTPError as exc:
        return int(exc.code), exc.read(4096) if exc.fp else b""


@dataclass(frozen=True)
class Ping:
    ok: bool
    detail: str


def ping_receiver(url: str, http_get: HttpGet = default_http_get, timeout: float = 10) -> Ping:
    """``GET`` the receiver URL: Specivo answers ``{"ok": true, "receiver": name}`` for an active one."""
    try:
        status, body = http_get(url, timeout)
    except (OSError, ValueError) as exc:
        return Ping(False, f"could not reach the receiver: {exc}")
    if status == 404:
        return Ping(False, "the receiver does not exist or is switched off (404)")
    if status != 200:
        return Ping(False, f"the receiver answered HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Ping(False, "the receiver answered something that is not JSON")
    if isinstance(payload, dict) and payload.get("ok") is True:
        return Ping(True, f"receiver {payload.get('receiver')!r} is active")
    return Ping(False, f"unexpected answer: {payload!r}")

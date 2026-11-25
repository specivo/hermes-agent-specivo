"""Specivo's REST API for the memory provider: search, wiki pages and issue comments.

Standard library only (``urllib``), with the API key as a Bearer token. Every call has a timeout
and a cap on the answer's size. Specivo answers as the key's account, so search returns only what
that account may see, and writes need its permissions (edit wiki pages, add issue notes).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

#: The header naming the Hermes session a call belongs to, so Specivo files it under that session.
SESSION_HEADER = "X-Agent-Session-Id"
#: The session id shape Specivo stores; anything else is not sent.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
#: The largest answer read from Specivo; a search page of 10 hits is a few kilobytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "hermes-agent-specivo"


class SpecivoApiError(RuntimeError):
    """A request failed. ``status`` is the HTTP status, or ``None`` when Specivo was not reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_conflict(self) -> bool:
        return self.status == 409

    @property
    def is_not_found(self) -> bool:
        return self.status == 404


@dataclass(frozen=True)
class WikiPage:
    title: str
    slug: str
    text: str
    lock_version: int


def _error_message(body: bytes, status: int) -> str:
    """The server's message from an error body, whatever shape it has; never the request."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return f"HTTP {status}"
    if isinstance(data, dict):
        for candidate in (data.get("error"), data.get("detail"), data):
            if isinstance(candidate, dict) and isinstance(candidate.get("message"), str):
                return str(candidate["message"])[:300]
            if isinstance(candidate, str):
                return candidate[:300]
    return f"HTTP {status}"


class RestClient:
    """One Specivo server and API key. Thread-safe: it holds no connection between calls."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0) -> None:
        self._api = f"{base_url.rstrip('/')}/api/v1"
        self._api_key = api_key
        self.timeout = timeout
        #: The Hermes session the calls belong to (sent as ``X-Agent-Session-Id``), when known.
        self.session_id: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self._api}{path}"
        if query:
            url = f"{url}?{urlencode({k: v for k, v in query.items() if v is not None})}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - the URL is https or loopback
        request.add_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        session_id = self.session_id
        if session_id and _SESSION_ID.fullmatch(session_id):
            request.add_header(SESSION_HEADER, session_id)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:  # noqa: S310
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            message = _error_message(exc.read(64 * 1024), status)
            raise SpecivoApiError(f"{method} {path} answered {status}: {message}", status) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SpecivoApiError(f"Specivo is unreachable: {reason}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SpecivoApiError(f"{method} {path}: the answer is larger than {MAX_RESPONSE_BYTES} bytes")
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SpecivoApiError(f"{method} {path}: the answer is not JSON") from None

    def search(
        self, query: str, *, project_key: str | None, limit: int, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Hybrid search over issues, wiki pages and comments the key's account may see."""
        data = self._request(
            "GET",
            "/search/",
            query={"q": query, "project_key": project_key, "mode": "hybrid", "scope": "all", "limit": limit},
            timeout=timeout,
        )
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def get_page(self, project_key: str, slug: str) -> WikiPage | None:
        """A wiki page, or ``None`` when it does not exist."""
        try:
            data = self._request("GET", f"/projects/{quote(project_key)}/wiki/{quote(slug)}/")
        except SpecivoApiError as exc:
            if exc.is_not_found:
                return None
            raise
        return _page(data)

    def create_page(self, project_key: str, title: str, text: str, comments: str) -> WikiPage:
        data = self._request(
            "POST",
            f"/projects/{quote(project_key)}/wiki/",
            body={"title": title, "text": text, "comments": comments},
        )
        return _page(data)

    def update_page(self, project_key: str, page: WikiPage, text: str, comments: str) -> WikiPage:
        """Save new text; :class:`SpecivoApiError` with status 409 when someone saved in between."""
        data = self._request(
            "PATCH",
            f"/projects/{quote(project_key)}/wiki/{quote(page.slug)}/",
            body={"text": text, "lock_version": page.lock_version, "comments": comments},
        )
        return _page(data)

    def add_comment(self, issue_ref: str, notes: str) -> None:
        self._request("POST", f"/issues/{quote(issue_ref)}/journals/", body={"notes": notes})


def _page(data: Any) -> WikiPage:
    if not isinstance(data, dict):
        raise SpecivoApiError("the wiki page answer is not an object")
    try:
        return WikiPage(
            title=str(data["title"]),
            slug=str(data["slug"]),
            text=str(data.get("text") or ""),
            lock_version=int(data["lock_version"]),
        )
    except (KeyError, TypeError, ValueError):
        raise SpecivoApiError("the wiki page answer lacks title, slug or lock_version") from None

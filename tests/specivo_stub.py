"""A stub of Specivo's REST API on 127.0.0.1 for the memory provider tests (no outside network)."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_WIKI_RE = re.compile(r"^/api/v1/projects/([^/]+)/wiki/(?:([^/]+)/)?$")
_JOURNAL_RE = re.compile(r"^/api/v1/issues/([^/]+)/journals/$")


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass
class StubSpecivo:
    """The state the handler serves. Tests change it between calls."""

    search_items: list[dict[str, Any]] = field(default_factory=list)
    #: project -> slug -> page dict (title, slug, text, lock_version)
    pages: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    comments: list[tuple[str, str]] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)
    #: Seconds every request waits before answering.
    delay: float = 0.0
    #: (method, path prefix) -> status to answer with instead of serving.
    refuse: dict[tuple[str, str], int] = field(default_factory=dict)
    #: How many PATCHes answer 409 as if someone else had saved first.
    conflicts: int = 0
    url: str = ""

    def calls(self, method: str | None = None, prefix: str = "") -> list[Recorded]:
        return [r for r in self.requests if (method is None or r.method == method) and r.path.startswith(prefix)]

    def wait_for(self, method: str, prefix: str, count: int = 1, timeout: float = 3.0) -> list[Recorded]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.calls(method, prefix)
            if len(found) >= count:
                return found
            time.sleep(0.01)
        raise AssertionError(f"no {method} {prefix} request arrived")


def _handler(stub: StubSpecivo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
            return None

        def _answer(self, status: int, payload: Any = None) -> None:
            body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._answer(status, {"error": {"code": "stub", "message": message}})

        def _handle(self) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            stub.requests.append(Recorded(self.command, parts.path, query, dict(self.headers.items()), body))
            if stub.delay:
                time.sleep(stub.delay)
            for (method, prefix), status in stub.refuse.items():
                if self.command == method and parts.path.startswith(prefix):
                    return self._error(status, "refused by the stub")
            if self.headers.get("Authorization") != "Bearer spv_test_key":
                return self._error(401, "Unauthorized")
            if parts.path == "/api/v1/search/" and self.command == "GET":
                limit = int(query.get("limit", "25"))
                items = stub.search_items[:limit]
                return self._answer(200, {"total_count": len(items), "offset": 0, "limit": limit, "items": items})
            wiki = _WIKI_RE.match(parts.path)
            if wiki:
                return self._wiki(unquote(wiki.group(1)), unquote(wiki.group(2) or ""), body)
            journal = _JOURNAL_RE.match(parts.path)
            if journal and self.command == "POST":
                stub.comments.append((unquote(journal.group(1)), str((body or {})["notes"])))
                return self._answer(201, {"id": len(stub.comments)})
            return self._error(404, "Not found")

        def _wiki(self, project: str, slug: str, body: Any) -> None:
            pages = stub.pages.setdefault(project, {})
            if self.command == "POST" and not slug:
                new_slug = re.sub(r"[^a-z0-9]+", "-", str(body["title"]).lower()).strip("-")
                if new_slug in pages:
                    return self._error(409, "exists")
                pages[new_slug] = {"title": body["title"], "slug": new_slug, "text": body["text"], "lock_version": 1}
                return self._answer(201, pages[new_slug])
            page = pages.get(slug)
            if page is None:
                return self._error(404, "Wiki page not found")
            if self.command == "GET":
                return self._answer(200, page)
            if self.command == "PATCH":
                if stub.conflicts > 0:
                    stub.conflicts -= 1
                    page["lock_version"] += 1  # someone else saved
                    return self._error(409, "Page has been modified by another user.")
                if body["lock_version"] != page["lock_version"]:
                    return self._error(409, "Page has been modified by another user.")
                page.update(text=body["text"], lock_version=page["lock_version"] + 1)
                return self._answer(200, page)
            return self._error(405, "Method not allowed")

        do_GET = do_POST = do_PATCH = _handle  # noqa: N815 - http.server's naming

    return Handler


@contextmanager
def running_stub() -> Iterator[StubSpecivo]:
    stub = StubSpecivo()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    server.daemon_threads = True
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()

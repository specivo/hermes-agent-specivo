"""Send Hermes's AI usage to Specivo when a session ends, by running the ai-usage reporter.

Off by default (``usage_report.enabled``). On ``on_session_end`` the plugin starts the reporter
(``usage_report.command``, by default ``ai-usage push --source hermes``) as a separate process:
Hermes never waits for it, and a run outlives a CLI that exits right away. At most one run starts
per ``usage_report.min_interval_seconds``, since a gateway ends a session after every exchange,
and never while the previous run is still going. A run longer than ``USAGE_TIMEOUT_SECONDS`` is
stopped; the reporter sends what it left on its next run.

The reporter gets ``SPECIVO_API_KEY`` from the plugin's secret scope, so the usage is reported
under the same account as the plugin's other calls. Its output is not kept or logged: the plugin
stores only when each run started and finished and its exit status, which ``hermes specivo
doctor`` and ``status`` show. The reporter's own ``status`` command has the details.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from .config import API_KEY_ENV, USAGE_TIMEOUT_SECONDS, UsageReportSettings, get_secret
from .runtime import Runtime
from .state import update_state

logger = logging.getLogger(__name__)

#: ``ctx.state`` key: ``{started, finished, exit_code, error}`` of the last run.
USAGE_STATE_KEY = "usage_report"

Spawn = Callable[..., Any]


def resolve_command(command: tuple[str, ...], which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """*command* with its program resolved on ``PATH`` (or as a path), or ``None`` when it is not there."""
    program = command[0]
    found = which(program)
    if found is None and os.sep in program and os.path.isfile(program) and os.access(program, os.X_OK):
        found = program
    return [found, *command[1:]] if found else None


def describe_last_run(state: dict[str, Any] | None) -> str:
    """One line on the last run, for ``doctor`` and ``status``."""
    if not state or not state.get("started"):
        return "no run yet"
    if state.get("error"):
        return f"last run could not start: {state['error']}"
    if state.get("finished") is None:
        return "a run is in progress"
    code = state.get("exit_code")
    if code == 0:
        return "last run sent everything"
    if code == 1:
        return "last run left records waiting; the next run sends them again"
    if code == "timeout":
        return f"last run was stopped after {USAGE_TIMEOUT_SECONDS}s"
    return f"last run failed (exit status {code}); run the reporter's status command for details"


class UsageReporter:
    """Runs the reporter on session end (see the module docstring)."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        spawn: Spawn = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.rt = runtime
        self._spawn = spawn
        self._which = which
        self._background = background or _daemon_thread

    def on_session_end(self, **_: Any) -> None:
        settings = self.rt.configured()
        if settings is None or not settings.usage_report.enabled:
            return
        try:
            self._maybe_run(settings.usage_report)
        except Exception:  # a hook never breaks Hermes
            logger.warning("specivo: could not start the ai-usage reporter", exc_info=True)

    def _maybe_run(self, usage: UsageReportSettings) -> None:
        now = self.rt.clock()
        claimed = False

        def claim(stored: Any) -> dict[str, Any]:
            nonlocal claimed
            last: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
            started = float(last.get("started") or 0)
            running = bool(started) and last.get("finished") is None and not last.get("error")
            # A run still going (unless it is older than the timeout: its process is gone) or a recent one.
            if running and now - started < USAGE_TIMEOUT_SECONDS + 60:
                return last
            if now - started < usage.min_interval_seconds:
                return last
            claimed = True
            return {"started": now, "finished": None, "exit_code": None, "error": None}

        update_state(self.rt.state, USAGE_STATE_KEY, {}, claim)
        if not claimed:
            return
        argv = resolve_command(usage.command, self._which)
        if argv is None:
            self._record(error=f"{usage.command[0]!r} not found")
            self.rt.warn_once(
                "usage:missing", "specivo: the ai-usage reporter %r was not found; usage is not sent", usage.command[0]
            )
            return
        env = dict(os.environ)
        key = get_secret(API_KEY_ENV)
        if key:
            env[API_KEY_ENV] = key
        try:
            process = self._spawn(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            self._record(error=type(exc).__name__)
            logger.warning("specivo: could not start the ai-usage reporter: %s", type(exc).__name__)
            return
        self._background(lambda: self._wait(process))

    def _wait(self, process: Any) -> None:
        try:
            code: int | str = process.wait(timeout=USAGE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = "timeout"
        self._record(finished=self.rt.clock(), exit_code=code)
        if code not in (0, 1):
            logger.warning("specivo: the ai-usage reporter ended with %s", code)

    def _record(self, **values: Any) -> None:
        def change(stored: Any) -> dict[str, Any]:
            last: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
            last.update(values)
            return last

        update_state(self.rt.state, USAGE_STATE_KEY, {}, change)


def _daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="specivo-usage-report", daemon=True).start()


__all__ = ["USAGE_STATE_KEY", "UsageReporter", "describe_last_run", "resolve_command"]

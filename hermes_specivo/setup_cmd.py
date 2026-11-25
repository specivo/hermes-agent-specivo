"""``hermes specivo setup | doctor | status``.

``setup`` writes, after showing the changes and asking (or with ``--yes``), and after a backup of
``config.yaml``:

- ``mcp_servers.<server>``: ``url: <specivo>/mcp/``, ``headers.Authorization: "Bearer ${SPECIVO_API_KEY}"``,
  ``timeout: 120`` and ``tools.exclude: [specivo_delete_*]`` (unless ``--allow-delete-tools``);
- ``plugins.entries.specivo.mcp_allowlist`` gains the server, so the plugin's hooks may call it;
- ``plugins.entries.specivo.settings``: ``url`` and, when given, ``default_project``;
- with ``--receiver-url``, the ``hooks.outbound`` entry named ``specivo``.

Everything else in ``config.yaml`` is left as it was, including keys the user added to these
blocks. Running it again with the same input changes nothing and writes nothing. ``--print`` only
prints the blocks. Secrets are never written or printed: Hermes asks for them at install
(``requires_env``) and the config refers to them by name.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from .config import API_KEY_ENV, DEFAULT_MCP_SERVER, WEBHOOK_SECRET_ENV, ConfigError, get_secret, normalize_base_url
from .kanban_sync import STATE_KEY as KANBAN_STATE_KEY
from .mcp import SpecivoError
from .runtime import Runtime
from .session_link import SESSIONS_KEY
from .usage_report import USAGE_STATE_KEY, describe_last_run, resolve_command
from .webhook import (
    EVENTS,
    TIMEOUT_SECONDS,
    HttpGet,
    default_http_get,
    find_entry,
    is_specivo_entry,
    normalize_receiver_url,
    outbound_entry,
    ping_receiver,
)

PLUGIN_ID = "specivo"
MCP_TIMEOUT = 120
DESTRUCTIVE_TOOLS = "specivo_delete_*"
AUTH_HEADER = f"Bearer ${{{API_KEY_ENV}}}"


# -- config.yaml access ------------------------------------------------------------------------


class ConfigStore(Protocol):
    path: Path

    def read(self) -> dict[str, Any]: ...

    def write(self, doc: dict[str, Any], written: set[tuple[str, ...]]) -> None: ...


class YamlConfigStore:
    """A plain YAML file. Used outside Hermes and in tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        import yaml

        if not self.path.exists():
            return {}
        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def write(self, doc: dict[str, Any], written: set[tuple[str, ...]]) -> None:
        import yaml

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(self.path)


class HermesConfigStore:
    """The active profile's ``config.yaml`` through ``hermes_cli.config`` (Hermes's locks, managed-install checks)."""

    def __init__(self) -> None:
        from hermes_cli import config as hermes_config

        self._config = hermes_config
        self.path = Path(hermes_config.get_config_path())

    def read(self) -> dict[str, Any]:
        data = self._config.read_user_config_raw(self.path)
        return dict(data) if isinstance(data, dict) else {}

    def write(self, doc: dict[str, Any], written: set[tuple[str, ...]]) -> None:
        self._config.save_config(doc, preserve_keys=written, merge_existing=True)


def default_store() -> ConfigStore:
    try:
        return HermesConfigStore()
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        return YamlConfigStore(home / "config.yaml")


def backup(path: Path, clock: Callable[[], float] = time.time) -> Path | None:
    """Copy ``config.yaml`` next to itself before a write; ``None`` when there is no file yet."""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    target = path.with_name(f"{path.name}.specivo-{stamp}.bak")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.specivo-{stamp}-{counter}.bak")
        counter += 1
    shutil.copy2(path, target)
    return target


# -- the merge ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupInput:
    url: str
    server: str = DEFAULT_MCP_SERVER
    receiver_url: str | None = None
    default_project: str | None = None
    exclude_destructive: bool = True
    plugin_id: str = PLUGIN_ID


def _child(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def merge(doc: dict[str, Any], spec: SetupInput) -> tuple[dict[str, Any], set[tuple[str, ...]]]:
    """*doc* with the plugin's blocks applied, and the paths written. *doc* itself is not changed."""
    new = copy.deepcopy(doc)
    written: set[tuple[str, ...]] = set()

    server = _child(_child(new, "mcp_servers"), spec.server)
    server["url"] = f"{spec.url}/mcp/"
    _child(server, "headers")["Authorization"] = AUTH_HEADER
    server.setdefault("timeout", MCP_TIMEOUT)
    written |= {("mcp_servers", spec.server, "url"), ("mcp_servers", spec.server, "headers", "Authorization")}
    written.add(("mcp_servers", spec.server, "timeout"))
    if spec.exclude_destructive:
        tools = _child(server, "tools")
        exclude = tools.get("exclude")
        exclude = list(exclude) if isinstance(exclude, list) else []
        if DESTRUCTIVE_TOOLS not in exclude:
            exclude.append(DESTRUCTIVE_TOOLS)
        tools["exclude"] = exclude
        written.add(("mcp_servers", spec.server, "tools", "exclude"))

    entry = _child(_child(_child(new, "plugins"), "entries"), spec.plugin_id)
    allow = entry.get("mcp_allowlist")
    allow = list(allow) if isinstance(allow, list) else []
    if spec.server not in allow:
        allow.append(spec.server)
    entry["mcp_allowlist"] = allow
    written.add(("plugins", "entries", spec.plugin_id, "mcp_allowlist"))
    settings = _child(entry, "settings")
    settings["url"] = spec.url
    written.add(("plugins", "entries", spec.plugin_id, "settings", "url"))
    if spec.server != DEFAULT_MCP_SERVER or "mcp_server" in settings:
        settings["mcp_server"] = spec.server
        written.add(("plugins", "entries", spec.plugin_id, "settings", "mcp_server"))
    if spec.default_project:
        settings["default_project"] = spec.default_project
        written.add(("plugins", "entries", spec.plugin_id, "settings", "default_project"))

    if spec.receiver_url:
        hooks = _child(new, "hooks")
        outbound = hooks.get("outbound")
        outbound = list(outbound) if isinstance(outbound, list) else []
        ours = outbound_entry(spec.receiver_url)
        index = next((i for i, item in enumerate(outbound) if is_specivo_entry(item)), None)
        if index is None:
            outbound.append(ours)
        else:
            kept = dict(outbound[index])
            kept.update({k: v for k, v in ours.items() if k != "timeout"})
            kept.setdefault("timeout", TIMEOUT_SECONDS)
            outbound[index] = kept
        hooks["outbound"] = outbound
        written.add(("hooks", "outbound"))
    return new, written


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(value, dict) and value:
        flat: dict[tuple[str, ...], Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, (*prefix, str(key))))
        return flat
    return {prefix: value}


def describe_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """One line per changed setting, ``path: old -> new`` (no secrets are ever in these values)."""
    old, new = _flatten(before), _flatten(after)
    lines = []
    for path in sorted(set(old) | set(new)):
        if old.get(path, _MISSING) == new.get(path, _MISSING):
            continue
        dotted = ".".join(path)
        if path not in old:
            lines.append(f"  + {dotted}: {json.dumps(new[path])}")
        elif path not in new:
            lines.append(f"  - {dotted}")
        else:
            lines.append(f"  ~ {dotted}: {json.dumps(old[path])} -> {json.dumps(new[path])}")
    return lines


_MISSING = object()


def blocks_yaml(doc: dict[str, Any], spec: SetupInput) -> str:
    """The plugin's blocks only, for ``--print``."""
    subset: dict[str, Any] = {
        "mcp_servers": {spec.server: doc["mcp_servers"][spec.server]},
        "plugins": {"entries": {spec.plugin_id: doc["plugins"]["entries"][spec.plugin_id]}},
    }
    if spec.receiver_url:
        subset["hooks"] = {"outbound": [find_entry(doc)]}
    try:
        import yaml
    except ImportError:
        return json.dumps(subset, indent=2)
    return str(yaml.safe_dump(subset, sort_keys=False, allow_unicode=True))


# -- commands ----------------------------------------------------------------------------------


@dataclass
class Io:
    out: TextIO = sys.stdout
    interactive: Callable[[], bool] = lambda: sys.stdin.isatty()
    ask: Callable[[str], str] = input


def run_setup(
    runtime: Runtime,
    args: argparse.Namespace,
    store: ConfigStore,
    io: Io,
    clock: Callable[[], float] = time.time,
) -> int:
    def say(text: str = "") -> None:
        print(text, file=io.out)

    current = runtime.settings()
    try:
        url = normalize_base_url(args.url) if args.url else (current.url if current else None)
        if url is None:
            raise ConfigError("give the Specivo address: hermes specivo setup --url https://your-specivo-host")
        receiver = normalize_receiver_url(args.receiver_url, url) if args.receiver_url else None
        project = args.project.strip().upper() if args.project else None
    except ConfigError as exc:
        say(f"error: {exc}")
        return 2
    spec = SetupInput(
        url=url,
        server=args.server_name or (current.mcp_server if current else DEFAULT_MCP_SERVER),
        receiver_url=receiver,
        default_project=project,
        exclude_destructive=not args.allow_delete_tools,
        plugin_id=str(getattr(runtime.ctx, "plugin_id", "") or PLUGIN_ID),
    )
    before = store.read()
    after, written = merge(before, spec)

    if args.print:
        say(f"# Add to {store.path}:")
        say(blocks_yaml(after, spec).rstrip())
        return 0

    changes = describe_changes(before, after)
    if not changes:
        say(f"{store.path} already has the Specivo settings; nothing to change.")
    else:
        say(f"Changes to {store.path}:")
        for line in changes:
            say(line)
        if not args.yes:
            if not io.interactive():
                say("Not written: no terminal to confirm on. Re-run with --yes to write, or --print to see the blocks.")
                return 2
            if io.ask("Write these changes (a backup is made first)? [y/N] ").strip().lower() not in ("y", "yes"):
                say("Nothing written.")
                return 1
        saved = backup(store.path, clock)
        store.write(after, written)
        say(f"Written. Backup: {saved}" if saved else "Written.")

    for name, needed in ((API_KEY_ENV, True), (WEBHOOK_SECRET_ENV, receiver is not None)):
        if needed and get_secret(name) is None:
            say(f"warning: {name} is not set. Add it with `hermes config set {name} ...` (it is never printed).")
    if changes:
        say("Restart Hermes (and the gateway, if you run one) so the MCP server and webhook are loaded.")
    say("Then run `hermes specivo doctor`.")
    return 0


@dataclass(frozen=True)
class Check:
    level: str  # "ok", "warn" or "fail"
    message: str


ConnectMcp = Callable[[str], None]


def hermes_connect_mcp(server: str) -> None:
    """Connect MCP ``server`` in this process.

    The gateway connects its MCP servers at startup, but a one-off CLI command such as
    ``hermes specivo doctor`` does not, so ``ctx.call_mcp`` would find no connection. Hermes's
    discovery skips servers that are already connected, so calling it twice is harmless.
    """
    from tools.mcp_tool_discovery import discover_mcp_tools

    discover_mcp_tools([server])


def doctor_checks(
    runtime: Runtime,
    store: ConfigStore,
    http_get: HttpGet = default_http_get,
    connect_mcp: ConnectMcp | None = None,
) -> list[Check]:
    checks: list[Check] = []
    settings = runtime.settings()
    if settings is None:
        try:
            from .config import load_settings

            load_settings(lambda key, default: runtime.ctx.get_config(key, default))
        except ConfigError as exc:
            return [Check("fail", f"plugin settings: {exc}")]
        return [Check("fail", "plugin settings could not be read")]
    if settings.url is None:
        return [Check("fail", "url is not set: run `hermes specivo setup --url https://your-specivo-host`")]
    checks.append(Check("ok", f"Specivo URL: {settings.url}"))

    doc = store.read()
    server = (doc.get("mcp_servers") or {}).get(settings.mcp_server)
    if not isinstance(server, dict):
        checks.append(Check("fail", f"mcp_servers.{settings.mcp_server} is missing from {store.path}: run setup"))
    else:
        if server.get("url") != f"{settings.url}/mcp/":
            checks.append(
                Check(
                    "fail",
                    f"mcp_servers.{settings.mcp_server}.url is {server.get('url')!r}, expected {settings.url}/mcp/",
                )
            )
        auth = (server.get("headers") or {}).get("Authorization")
        if auth != AUTH_HEADER:
            checks.append(
                Check(
                    "warn",
                    f"mcp_servers.{settings.mcp_server}.headers.Authorization should be "
                    f"{AUTH_HEADER!r} so the key stays in the environment",
                )
            )
        if server.get("trust") == "untrusted":
            checks.append(
                Check(
                    "warn",
                    "the MCP server is `trust: untrusted`: the plugin's writes need approval "
                    "and are refused in unattended runs",
                )
            )
    plugin_id = str(getattr(runtime.ctx, "plugin_id", "") or PLUGIN_ID)
    entry = ((doc.get("plugins") or {}).get("entries") or {}).get(plugin_id) or {}
    if settings.mcp_server not in (entry.get("mcp_allowlist") or []):
        checks.append(Check("fail", f"plugins.entries.{plugin_id}.mcp_allowlist lacks {settings.mcp_server!r}"))

    if get_secret(API_KEY_ENV) is None:
        checks.append(Check("fail", f"{API_KEY_ENV} is not set"))
    else:
        checks.append(Check("ok", f"{API_KEY_ENV} is set"))
        try:
            if connect_mcp is not None:
                try:
                    connect_mcp(settings.mcp_server)
                except Exception as exc:  # the whoami call below reports what is still wrong
                    checks.append(Check("warn", f"could not connect MCP server {settings.mcp_server!r}: {exc}"))
            who = runtime.client(settings).whoami()
        except SpecivoError as exc:
            checks.append(Check("fail", f"specivo_whoami: {exc}"))
        else:
            login = next((ln.split(":", 1)[1].strip() for ln in who.splitlines() if ln.startswith("login:")), "?")
            checks.append(Check("ok", f"MCP works: signed in to Specivo as {login}"))

    if settings.events_enabled:
        hook = find_entry(doc)
        if hook is None:
            checks.append(
                Check(
                    "warn", "no hooks.outbound entry for Specivo: run setup with --receiver-url to send session events"
                )
            )
        else:
            missing = sorted(set(EVENTS) - set(hook.get("events") or []))
            if missing:
                checks.append(Check("warn", f"the Specivo webhook does not send {', '.join(missing)}"))
            if hook.get("secret_env") != WEBHOOK_SECRET_ENV:
                checks.append(Check("fail", f"the Specivo webhook must use secret_env: {WEBHOOK_SECRET_ENV}"))
            elif get_secret(WEBHOOK_SECRET_ENV) is None:
                checks.append(
                    Check(
                        "fail",
                        f"{WEBHOOK_SECRET_ENV} is not set: deliveries would be unsigned and Specivo refuses them",
                    )
                )
            url = str(hook.get("url") or "")
            ping = ping_receiver(url, http_get)
            checks.append(Check("ok" if ping.ok else "fail", f"receiver {url}: {ping.detail}"))

    sync = settings.issue_sync
    if sync.enabled:
        checks.append(Check("ok", f"Kanban sync on: project {settings.sync_project()}, every {sync.poll_seconds}s"))

    usage = settings.usage_report
    if usage.enabled:
        last = runtime.state.get(USAGE_STATE_KEY, {}) or {}
        if resolve_command(usage.command) is None:
            checks.append(Check("fail", f"usage reporting on, but {usage.command[0]!r} is not found"))
        else:
            level = "warn" if last.get("error") or last.get("exit_code") not in (None, 0, 1) else "ok"
            checks.append(
                Check(
                    level, f"usage reporting on, at most every {usage.min_interval_seconds}s: {describe_last_run(last)}"
                )
            )
    return checks


def run_doctor(
    runtime: Runtime,
    store: ConfigStore,
    io: Io,
    http_get: HttpGet = default_http_get,
    connect_mcp: ConnectMcp | None = None,
) -> int:
    checks = doctor_checks(runtime, store, http_get, connect_mcp)
    for check in checks:
        print(f"[{check.level.upper():4}] {check.message}", file=io.out)
    failed = any(c.level == "fail" for c in checks)
    print("Some checks failed." if failed else "All required checks passed.", file=io.out)
    return 1 if failed else 0


def run_status(runtime: Runtime, io: Io) -> int:
    def say(text: str) -> None:
        print(text, file=io.out)

    settings = runtime.settings()
    if settings is None:
        say("Plugin settings are invalid; run `hermes specivo doctor`.")
        return 1
    say(f"Specivo URL: {settings.url or '(not set)'}")
    say(f"MCP server: {settings.mcp_server}")
    say(f"Default project: {settings.default_project or '(none)'}")
    say(f"Context line: {'on' if settings.context_inject else 'off'}")
    sessions = runtime.state.get(SESSIONS_KEY, {}) or {}
    with_issue = sum(1 for e in sessions.values() if isinstance(e, dict) and e.get("issue"))
    say(f"Sessions seen: {len(sessions)} ({with_issue} with a current issue)")
    sync = settings.issue_sync
    say(f"Kanban sync: {'on' if sync.enabled else 'off'}")
    if sync.enabled:
        kanban = runtime.state.get(KANBAN_STATE_KEY, {}) or {}
        cards = kanban.get("cards") or {}
        by_state: dict[str, int] = {}
        for card in cards.values():
            by_state[str(card.get("state"))] = by_state.get(str(card.get("state")), 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(by_state.items())) or "none"
        last = float(kanban.get("last_poll") or 0)
        say(f"  cards: {summary}")
        say(f"  last poll: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last)) if last else 'never'}")
    usage = settings.usage_report
    say(f"Usage reporting: {'on' if usage.enabled else 'off'}")
    if usage.enabled:
        report = runtime.state.get(USAGE_STATE_KEY, {}) or {}
        started = float(report.get("started") or 0)
        say(f"  last run: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)) if started else 'never'}")
        say(f"  {describe_last_run(report)}")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="specivo_command")
    setup = subs.add_parser("setup", help="Write the Specivo MCP server, plugin settings and webhook into config.yaml")
    setup.add_argument("--url", help="Specivo base address, e.g. https://specivo.example.com")
    setup.add_argument("--receiver-url", help="Hermes receiver URL from Specivo (…/api/v1/hermes/events/<token>/)")
    setup.add_argument("--project", help="Default project key, e.g. ACME")
    setup.add_argument("--server-name", help=f"Name of the MCP server entry (default {DEFAULT_MCP_SERVER})")
    setup.add_argument("--allow-delete-tools", action="store_true", help="Do not exclude specivo_delete_* tools")
    setup.add_argument("--yes", action="store_true", help="Write without asking")
    setup.add_argument("--print", action="store_true", help="Only print the config blocks; write nothing")
    subs.add_parser("doctor", help="Check the configuration, the API key, MCP access and the receiver")
    subs.add_parser("status", help="Show settings, linked sessions and Kanban sync state")


def make_handler(runtime: Runtime) -> Callable[[argparse.Namespace], int]:
    def handle(args: argparse.Namespace) -> int:
        command = getattr(args, "specivo_command", None)
        io = Io()
        if command == "setup":
            return run_setup(runtime, args, default_store(), io)
        if command == "doctor":
            return run_doctor(runtime, default_store(), io, connect_mcp=hermes_connect_mcp)
        if command == "status":
            return run_status(runtime, io)
        print("Usage: hermes specivo {setup,doctor,status}", file=io.out)
        return 2

    return handle

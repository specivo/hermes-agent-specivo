# Specivo plugin for Hermes Agent

This is a native [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that makes
[Specivo](https://specivo.io) the agent's issue tracker, wiki and cost ledger:

- **One setup command.** `hermes specivo setup` writes the Specivo MCP server, the plugin's MCP
  permission and the signed event webhook into Hermes's `config.yaml`. It shows the changes, asks,
  and backs up the file first.
- **Current issue.** Each Hermes session is linked to its Specivo agent session by Hermes's own
  `session_id` (and its parent's). The plugin keeps track of the issue the session works on, so
  AI usage and session events land on the right issue. Specivo's **Agent sessions** page and the
  issue's **Agents** tab show them.
- **Kanban sync (optional).** Open Specivo issues become Hermes Kanban cards. A claimed card moves
  its issue to In Progress, a completed card posts its summary and resolves the issue, and a
  blocked card posts the reason and moves the issue to Feedback.
- **`hermes specivo doctor`.** It checks the configuration, the API key, MCP access and the receiver.

Requirements: Hermes Agent 0.23.3 or later and Specivo 0.15.0 or later. The plugin uses only the
standard library and Hermes's public plugin API.

Hermes Agent is a product of Nous Research. This plugin is not affiliated with or endorsed by Nous
Research.

## Install

```bash
hermes plugins install specivo/hermes-agent-specivo --ref <commit>
hermes plugins enable specivo
```

During the install, Hermes asks for the two secrets the plugin declares. It stores them in its
`.env`; the plugin never stores or prints them:

| Variable | What it is |
|---|---|
| `SPECIVO_API_KEY` | A Specivo API key (see below) |
| `SPECIVO_WEBHOOK_SECRET` | The secret of your Hermes receiver in Specivo (see below) |

To change one later, run `hermes config set SPECIVO_API_KEY <value>`.

## Create the API key and the receiver in Specivo

1. **API key.** In Specivo, create an API key for the account Hermes works as: your profile's API
   keys page, or a service account an administrator creates for the agent. Anything the agent does
   in Specivo is done as this account.
2. **Receiver.** Open **Hermes receivers** (`/my/hermes-receivers/`) and create one. Specivo shows
   its URL (`https://your-specivo-host/api/v1/hermes/events/<token>/`) and its secret **once**. You
   can also create one over the API:

   ```bash
   curl -X POST https://your-specivo-host/api/v1/my/hermes-receivers/ \
     -H "Authorization: Bearer $SPECIVO_API_KEY" -H "Content-Type: application/json" \
     -d '{"name": "laptop"}'
   ```

   The answer has `url`, `secret` and a ready-made `hooks_config` block. The plugin writes that
   block for you, so you only need `url` and `secret`.

## Set up

```bash
hermes specivo setup --url https://your-specivo-host \
  --receiver-url https://your-specivo-host/api/v1/hermes/events/<token>/ \
  --project ACME
```

`setup` prints each change and asks before it writes anything. It writes:

```yaml
mcp_servers:
  specivo:
    url: https://your-specivo-host/mcp/
    headers:
      Authorization: Bearer ${SPECIVO_API_KEY}   # the key stays in .env
    timeout: 120
    tools:
      exclude: [specivo_delete_*]                # leave out with --allow-delete-tools
plugins:
  entries:
    specivo:
      mcp_allowlist: [specivo]                   # lets the plugin's hooks call the server
      settings:
        url: https://your-specivo-host
        default_project: ACME
hooks:
  outbound:
    - name: specivo
      url: https://your-specivo-host/api/v1/hermes/events/<token>/
      events: [on_session_start, post_tool_call, pre_approval_request, post_approval_response,
               subagent_start, subagent_stop, on_session_end]
      secret_env: SPECIVO_WEBHOOK_SECRET
      timeout: 10
```

What `setup` does with your existing `config.yaml`:

- Nothing else in the file changes. Keys you added to these blocks stay too, for example `trust`,
  your own `tools.exclude` entries or another `timeout`.
- Before writing, it saves a backup next to the file as `config.yaml.specivo-<time>.bak`.
- Running it again with the same input changes nothing and writes nothing.
- `--print` shows the blocks without writing. `--yes` writes without asking; without `--yes`,
  `setup` writes only after you confirm in a terminal.

After setup, restart Hermes (and the gateway, if you run one) and run:

```bash
hermes specivo doctor
```

`doctor` checks the following:

- the URL;
- the `mcp_servers` entry and the allowlist;
- that both secrets are set (it never prints them);
- that `specivo_whoami` works over MCP, and which account it answers for;
- that the webhook entry is present;
- that the receiver answers `GET` with `{"ok": true}`.

It exits with code 1 if any required check fails. `hermes specivo status` shows the settings, how
many sessions the plugin has linked, and the Kanban sync state.

Plain `http://` is accepted only for a Specivo server on `localhost` or a loopback address.

## Configuration

All settings live under `plugins.entries.specivo.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `url` | (none) | The Specivo base address. `setup` sets it |
| `mcp_server` | `specivo` | Name of the `mcp_servers` entry (`setup --server-name`) |
| `default_project` | (none) | Project key the prompt mentions and the Kanban sync falls back to |
| `context.inject` | `true` | Adds a short Specivo section to the system prompt and a `Current Specivo issue: ACME-12 (In Progress)` line to each turn |
| `events.enabled` | `true` | Whether `doctor` checks the webhook |
| `issue_sync.enabled` | `false` | Turns on the Kanban sync |
| `issue_sync.project` | `default_project` | Project whose issues become cards |
| `issue_sync.select.assignee` | `me` | Always `me`: only issues assigned to the API key's account become cards (any other value is refused) |
| `issue_sync.select.tag` | (none) | Also require this tag |
| `issue_sync.board` | (all) | Only this Kanban board; cards are created on it |
| `issue_sync.card_assignee` | the plugin's profile | Hermes profile the cards are assigned to |
| `issue_sync.poll_seconds` | `60` | How often to look for new issues (15 or more) |
| `issue_sync.statuses` | `In Progress`, `Resolved`, `Feedback` | `in_progress`, `resolved`, `feedback`: status names or ids for claim, completion and block |

A bad value makes the hooks go idle, logs one warning naming the key, and makes `doctor` fail
with the same message.

## Current issue

On the first turn of a session, and the first time it sees a resumed session, the plugin calls
`specivo_agent_session(op="set")` with Hermes's `session_id` and parent session id. Specivo then
joins AI usage (from the [AI usage reporter](https://github.com/specivo/ai-usage-reporter)) and
webhook events to that agent session by id.

The current issue is set when the agent:

- changes an issue's status or assignee with `specivo_update_issue`;
- starts a timer with `specivo_timer`;
- creates an issue;
- calls `specivo_agent_session` with an issue.

Specivo records the issue itself; the plugin keeps a copy only to add the context line. Resolving
or closing the current issue clears it. You can also set it yourself:

```text
/specivo issue ACME-12    set the issue this session works on (also tells Specivo)
/specivo issue clear      forget it for this session
/specivo status           the Hermes session, the issue and Specivo's agent session
```

## Kanban sync

Hermes has no hook for a new Kanban task, so the sync polls on the Kanban dispatcher's tick, at
most every `poll_seconds`:

| When | The plugin |
|---|---|
| An open issue assigned to the plugin's account (and tagged, if `select.tag` is set) has no card | creates one with `kanban_create`: title `Specivo issue ACME-12`, a body that links to the issue, assigned to `card_assignee`. It creates at most 20 per poll |
| `kanban_task_claimed` | moves the issue to `in_progress` |
| `kanban_task_completed` | posts the summary as a comment and moves the issue to `resolved` |
| `kanban_task_blocked` | posts the reason as a comment and moves the issue to `feedback` |
| The issue is resolved, closed or no longer matches | completes its card, without a comment |

Each transition runs once per card. A Specivo call that fails is retried on the next tick.
Comments end with "via Hermes"; the plugin never reads them back.

Hermes fires `kanban_task_claimed` in the dispatcher and the other two hooks in the worker process,
which runs as the card's assignee profile. That profile needs the plugin enabled with the same
`url`, the same `SPECIVO_API_KEY`, and `issue_sync.enabled: true`.

A worker recognises a card the plugin made by the `specivo-issue: ACME-12 sig=...` line in its
body. The signature is derived from `SPECIVO_API_KEY`, so someone who can create cards but does not
have the key cannot forge one. The worker adopts the card only if all of these hold:

- the signature checks;
- the issue is in the sync project;
- the issue is in the current selection (open and assigned to the plugin's account);
- no other card is already mapped to the issue.

Any other card is ignored. Rotating the API key invalidates the signatures on existing cards,
so only the profile that created those cards can still act on them.

## Privacy and network use

What the plugin sends, and where:

- **Your Specivo server only**, through the MCP server Hermes connects to. The plugin makes no
  other network calls, except `doctor`'s `GET` on your receiver URL. It has no telemetry.
- **Session link.** The plugin sends Hermes's session id, the parent session id, and the issue key
  when there is one.
- **Kanban sync.** The plugin reads issue keys and statuses and puts only the key on a card. It
  posts card summaries and block reasons as issue comments, after Hermes's secret redaction
  (`redact_text` at full strength). If redaction is unavailable or fails, the text is not sent;
  the comment says the summary was withheld.
- **Webhook.** Hermes itself sends events to the receiver, signed with your webhook secret. The
  Specivo receiver documentation lists the fields it stores.
- **Local data.** The plugin writes `config.yaml` only when you run `setup` (with a backup), and
  its own state (linked sessions, cards) in Hermes's plugin data folder. Secrets stay in Hermes's
  `.env` and are read through Hermes's profile secret scope.

Cards carry no issue text: the title is the key and the body is a link. Cards are made only for
issues assigned to the plugin's account, and assigning an issue needs edit rights in the project.
A worker that opens the issue still reads text that other people wrote, so the card tells it to
treat that text as information, not as instructions.

The plugin's own calls go through the MCP server like the agent's. If you mark that server
`trust: untrusted`, Hermes asks before each write, and in unattended runs (cron, Kanban workers)
those writes are refused.

## Limits

- Specivo keeps one agent session per MCP connection. Several Hermes sessions that share one
  connection, for example subagents or a gateway serving several chats, take turns setting it. The
  last one to link wins.
- A card completed in Hermes is not created again while it stays on the board. If its issue is
  reopened in Specivo, the card has to be moved back on the board by hand.

## Development

```bash
uv sync
make test        # pytest, no network; fakes for ctx, the Specivo MCP server and the Kanban board
make lint        # ruff and mypy
make hermes-validate   # the plugin catalog's checks against the pinned Hermes release (needs network)
```

The repository root is the plugin directory: `plugin.yaml` and `__init__.py`, with the code in
`hermes_specivo/`. `pyproject.toml` only configures the development tools. It has no `[project]`
table, so Hermes installs nothing for the plugin.

## License

MIT. See [LICENSE](LICENSE).

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
- **Memory provider (optional).** With `memory.provider: specivo`, Hermes recalls matching Specivo
  issues and wiki pages before each turn, and the agent can save curated notes to a wiki page or an
  issue comment. It never stores conversation turns.
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
| `issue_sync.board` | (the board Hermes names) | Only this Kanban board slug; cards are created on it. Recommended: without it, the plugin uses the board each dispatcher tick names |
| `issue_sync.card_assignee` | the plugin's profile | Hermes profile the cards are assigned to |
| `issue_sync.poll_seconds` | `60` | How often to look for new issues (15 or more) |
| `issue_sync.statuses` | `In Progress`, `Resolved`, `Feedback` | `in_progress`, `resolved`, `feedback`: status names or ids for claim, completion and block |

A bad value makes the hooks go idle, logs one warning naming the key, and makes `doctor` fail
with the same message.

## Current issue

Before every turn the plugin calls `specivo_agent_session(op="set")` with Hermes's `session_id`.
Specivo keeps one agent session per Hermes session on the MCP connection and makes it the current
one, so subagents and gateway chats that share one connection each get their own Specivo agent
session. The first call for a session (a new one, or a resumed one the plugin has not seen) also
sends the parent session id. Specivo then joins AI usage (from the
[AI usage reporter](https://github.com/specivo/ai-usage-reporter)) and webhook events to that agent
session by id. This needs Specivo 0.15.0 or later.

Tool calls that run at the same time on one connection still go to whichever session was selected
last. If that matters, for example for subagents that run in parallel, give each its own
`mcp_servers` entry, so each has its own connection.

The current issue is set when the agent:

- changes an issue's status or assignee with `specivo_update_issue`;
- starts a timer with `specivo_timer`;
- creates an issue;
- calls `specivo_agent_session` with an issue.

Specivo records the issue itself; the plugin keeps a copy only to add the context line. Resolving
or closing the current issue clears it, here and on Specivo, and so does a `specivo_agent_session`
set with an empty `issue_ref`. You can also set it yourself:

```text
/specivo issue ACME-12    set the issue this session works on (also tells Specivo)
/specivo issue clear      clear it for this session (also tells Specivo)
/specivo status           the Hermes session, the issue and this session's Specivo agent session
```

## Kanban sync

Hermes has no hook for a new Kanban task, so the sync polls on the Kanban dispatcher's tick, at
most every `poll_seconds`:

| When | The plugin |
|---|---|
| An open issue assigned to the plugin's account (and tagged, if `select.tag` is set) has no card | creates one with `kanban_create`: title `Specivo issue ACME-12`, a body that links to the issue, assigned to `card_assignee`. It creates at most 20 per poll |
| `kanban_task_claimed` | moves the issue to `in_progress` |
| `kanban_task_completed` | posts the worker run's summary as a comment and moves the issue to `resolved` |
| `kanban_task_blocked` | posts the worker run's reason as a comment and moves the issue to `feedback` |
| The issue is resolved, closed or no longer matches | completes its card, without a comment |

Each transition runs once per card. A Specivo call that fails is retried on the next tick.
Comments end with "via Hermes"; the plugin never reads them back.

Hermes fires `kanban_task_claimed` in the dispatcher and the other two hooks in the worker process,
which runs as the card's assignee profile. That profile needs the plugin enabled with the same
`url`, the same `SPECIVO_API_KEY`, and `issue_sync.enabled: true`.

### Boards

Every Kanban call the plugin makes names its board. The board is `issue_sync.board` when it is set.
Otherwise it is the board Hermes names in the dispatcher tick, which is Hermes's own idea of the
current board. A tick that names no board, when none is configured, syncs nothing. A lifecycle hook
without a board is used only when the board is unambiguous: either one is configured, or the ticks
have named exactly one. Task ids are unique only within a board, so the plugin identifies cards as
`board/task id`.

### Which cards the plugin trusts

The plugin assumes that someone without the API key may do any of these:

- create cards;
- comment on any card;
- complete or block cards they can reach;
- make lifecycle hooks fire with any payload.

It protects itself in four ways.

**Its own cards carry a proof.**

- The card's body names its issue in exactly one `specivo-issue: ACME-12` line, which must be the
  body's last line.
- Right after creating the card, the plugin appends a comment `specivo-card: ACME-12 sig=...`.
  Hermes cannot edit a card body, and comments are append-only.
- The signature is an HMAC of the issue key, the board and the card's own id, under a key derived
  from `SPECIVO_API_KEY`. Without the key it cannot be forged, and a signature copied onto another
  card does not verify there, on any board.

**It checks the card it creates.**

- `kanban_create` uses an idempotency key derived from `SPECIVO_API_KEY`, so nobody can create a
  card in advance that Hermes would hand back to the plugin.
- The plugin reads the returned card back on the same board. If the board, title, body or assignee
  differ from what it sent, or the card has comments the plugin did not write, it refuses the card
  and logs a warning.

**It adopts another profile's card only if all of these hold:**

- the signature checks for this card on this board;
- the issue is in the sync project;
- the issue is in the current selection (open and assigned to the plugin's account);
- no other card is already mapped to the issue.

**It believes the card, not the hook.**

- A hook counts only if the card, read back on its board, is in the state the hook reports:
  running after a claim, done after a completion, blocked after a block.
- The comment text is the summary of the card's own run, never the text in the hook payload.
- The run's text is used only when it is the worker's run, which means all of these hold:
  - it has a duration;
  - Hermes recorded the expected worker profile on it (`card_assignee`, else the plugin's own
    profile; Hermes sets this itself when the claim opens the run, and no caller can pass it);
  - the card is still assigned to that profile.
- Any other completion or block still moves the issue, for example an unclaimed card someone
  finished by hand, or a card reassigned to another profile. The comment then says the card was
  finished outside a worker run and does not copy its text.

Any other card or hook is ignored and a warning is logged. If the proof comment cannot be posted,
the plugin retries on the next tick. Rotating the API key invalidates the signatures on existing
cards, so only the profile that created those cards can still act on them.

## Memory provider

The plugin also offers a Hermes memory provider. It is off until you select it:

```yaml
memory:
  provider: specivo
plugins:
  entries:
    specivo:
      settings:
        url: https://your-specivo-host
        default_project: ACME
        memory:
          project: ACME                              # optional; see "Which project" below
          cwd_projects: {"/srv/work/acme": "ACME"}
          chat_projects: {"telegram:12345": "OPS"}
          notes_page: Agent Notes
          max_hits: 5                                # 1 to 10
          max_chars: 1800                            # 200 to 8000
          mirror_builtin: false
```

You can also pick it with `hermes memory setup`, which asks for `project`, `notes_page`,
`max_hits` and `max_chars`. The provider needs the plugin's `url` and `SPECIVO_API_KEY`; it reads
both from the same places as the rest of the plugin. Restart Hermes after changing the selection.

What it does:

- **Recall.** Before each turn Hermes asks the provider for context. The provider runs a hybrid
  (keyword and semantic) Specivo search with your last message and returns at most `max_hits`
  hits and `max_chars` characters: one line per issue, wiki page or comment, with a short snippet
  and a link. Searches run in the background after a turn and are used on the next one. On the
  first turn of a session the provider searches right away and waits at most two seconds. Short
  replies ("ok", "thanks") and slash commands are not searched. If Specivo is slow or down, the turn
  goes on without recall and the provider logs one line; it never blocks or fails a turn.
- **`specivo_remember`** (`text`, `kind`: note, decision or fact, optional `issue_ref`). This tool
  appends a dated line under today's `## YYYY-MM-DD` heading of the project's notes page
  (`notes_page`, created on first use). With `issue_ref` it posts an issue comment instead. Notes
  are limited to 2,000 characters. The tool is refused in cron runs, subagents and the
  pre-compression flush.
- **`specivo_recall`** (`query`, `scope`: project or all, `limit` up to 10). An explicit search that
  returns results in the same short format.
- **Mirror (off by default).** With `mirror_builtin: true`, additions and replacements in Hermes's
  built-in MEMORY.md and USER.md are also appended under `## Memory mirror` on the notes page.

Which project: `memory.project`, else the longest `memory.cwd_projects` prefix of the session's
working directory, else `memory.chat_projects` for `<platform>:<chat id>`, else `default_project`.
With no project, there is no recall and the tools say what to set.

Things to know before you choose it:

- **One external memory provider per profile.** Hermes runs its built-in memory (MEMORY.md and
  USER.md) next to one external provider. Selecting `specivo` means not using Mem0, Honcho or
  another external provider in that profile.
- **Permissions are Specivo's.** The provider acts as the API key's account. Search returns only
  what that account may see, including the key's project limit. Notes need permission to edit wiki
  pages in the project, and comments need permission to add issue notes. Refusals come back to the
  agent as a tool error. Specivo records each search in its audit log (one per turn), and the notes
  page history shows the account's edits.
- **Recalled text is reference material.** Issue and wiki text is written by people, so the
  recall block is labelled as reference material, not instructions.
- **No turns are stored.** The provider never sends the conversation to Specivo. It sends only the
  search query, the notes the agent chooses to save, and mirrored memory entries when you turn
  mirroring on.

## Privacy and network use

What the plugin sends, and where:

- **Your Specivo server only.** The hooks go through the MCP server Hermes connects to. The memory
  provider, when selected, calls Specivo's REST API (`/api/v1/`) with `SPECIVO_API_KEY`, because
  Hermes's memory loader gives it no MCP access. The plugin makes no other network calls, except
  `doctor`'s `GET` on your receiver URL. It has no telemetry.
- **Session link.** The plugin sends Hermes's session id, the parent session id, and the issue key
  when there is one.
- **Kanban sync.** The plugin reads issue keys and statuses and puts only the key on a card. It
  posts card summaries and block reasons as issue comments, after Hermes's secret redaction
  (`redact_text` at full strength). If redaction is unavailable or fails, the text is not sent;
  the comment says the summary was withheld.
- **Memory provider.** The search query is your last message, after Hermes's secret scrub and
  then the plugin's own redaction, cut to 300 characters. Notes, comments and mirrored memory
  entries are redacted (`redact_text` at full strength) before they are sent. If redaction is
  unavailable or fails, nothing is sent: the search is skipped, or the tool answers that the note
  was not saved, and a line is logged. Conversation turns are never sent.
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
- Hermes lets anyone with Kanban rights complete or block a card once its worker has exited. The
  run they finish is a real run, so its text is posted after redaction, like a worker's. The plugin
  cannot tell the two apart.
- Someone who comments on a new card in the moment between its creation and the plugin reading it
  back makes the plugin refuse its own card. That blocks the sync for that issue; it gives the
  commenter no control over it.

## Development

```bash
make install     # uv sync --project devtools
make test        # pytest, no outside network: fakes for ctx, the MCP server and the Kanban board,
                 # and a stub of the Specivo REST API on 127.0.0.1
make lint        # ruff and mypy
make hermes-validate   # the plugin catalog's checks against Hermes v0.23.3 (needs network)
```

The repository root is the plugin directory: `plugin.yaml` and `__init__.py`, with the code in
`hermes_specivo/`. The root `pyproject.toml` only configures pytest, ruff and mypy. It has no
`[project]` table, so Hermes installs nothing for the plugin. The tools themselves, and the
supported Pythons (3.11 to 3.14), are pinned in `devtools/pyproject.toml` and `devtools/uv.lock`.

`make hermes-validate` installs Hermes the documented way, with its own `install.sh` pinned to the
v0.23.3 release commit. It uses a scratch `HOME` under `.hermes-check/`, so it never touches your own
Hermes or shell setup. Hermes cannot be installed with `pip` or `uv tool install`: its `setup.py`
refuses to build a wheel outside its Nix build.

## License

MIT. See [LICENSE](LICENSE).

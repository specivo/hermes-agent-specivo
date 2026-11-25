# Changelog

Release notes of the Specivo plugin for Hermes Agent. Install a release with
`hermes plugins install specivo/hermes-agent-specivo --ref <commit>`.

## 0.2.0

Needs Specivo 0.15.0 or later.

- **AI usage reporting** (`usage_report.enabled`): the plugin runs the ai-usage reporter when a
  session ends, so Hermes's usage reaches Specivo without a cron job. At most one run per
  `min_interval_seconds` (default 300), never two at once, stopped after ten minutes; `doctor` and
  `status` show the last run.
- The Kanban sync polls every five minutes by default (was every minute), and reads issues over
  the REST API marked as background housekeeping, so its polls no longer count as tool calls of the
  current conversation or keep its Specivo agent session active.
- The memory provider names the Hermes session in its REST calls (`X-Agent-Session-Id`), so they
  are filed under the conversation's agent session.
- `doctor` connects the MCP server before its `whoami` check.
- Each Hermes session now has its own Specivo agent session, also when subagents and gateway chats
  share one MCP connection: every turn selects the session by its id. Calls that run at the same
  time on one connection still go to the session selected last. `/specivo issue clear` and
  resolving the current issue now clear the working issue on Specivo too, and `/specivo status`
  shows the session's own agent session.
- Optional memory provider (`memory.provider: specivo`). It recalls Specivo issues and wiki pages
  before each turn and adds the `specivo_remember` and `specivo_recall` tools. It can also mirror
  built-in memory writes (off by default). It never stores conversation turns.

## 0.1.0

First release: `hermes specivo setup | doctor | status`, the session link and current issue
(`/specivo issue`, `/specivo status`), and the optional Kanban sync.

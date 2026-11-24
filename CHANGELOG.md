# Changelog

Release notes of the Specivo plugin for Hermes Agent. Install a release with
`hermes plugins install specivo/hermes-agent-specivo --ref <commit>`.

## Unreleased

- Each Hermes session now has its own Specivo agent session, also when subagents and gateway chats
  share one MCP connection: every turn selects the session by its id. Calls that run at the same
  time on one connection still go to the session selected last. `/specivo issue clear` and
  resolving the current issue now clear the working issue on Specivo too, and `/specivo status`
  shows the session's own agent session. Needs Specivo 0.15.0 or later.
- Optional memory provider (`memory.provider: specivo`). It recalls Specivo issues and wiki pages
  before each turn and adds the `specivo_remember` and `specivo_recall` tools. It can also mirror
  built-in memory writes (off by default). It never stores conversation turns.

## 0.1.0

First release: `hermes specivo setup | doctor | status`, the session link and current issue
(`/specivo issue`, `/specivo status`), and the optional Kanban sync.

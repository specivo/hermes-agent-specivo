# Changelog

Release notes of the Specivo plugin for Hermes Agent. Install a release with
`hermes plugins install specivo/hermes-agent-specivo --ref <commit>`.

## Unreleased

- Optional memory provider (`memory.provider: specivo`). It recalls Specivo issues and wiki pages
  before each turn and adds the `specivo_remember` and `specivo_recall` tools. It can also mirror
  built-in memory writes (off by default). It never stores conversation turns.

## 0.1.0

First release: `hermes specivo setup | doctor | status`, the session link and current issue
(`/specivo issue`, `/specivo status`), and the optional Kanban sync.

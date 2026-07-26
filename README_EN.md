# Agent Team Workflow

[中文](README.md)

Agent Team Workflow is a durable multi-agent development protocol for long-running software projects. It separates architecture leadership, implementation orchestration, and independent review into three persistent roles, with on-disk state for recovery across processes and context windows.

> Version **0.1.0 alpha**. Codex + CC-Panes is the first reference implementation. The core protocol is intended to support additional CLI adapters in the future.

## Team model

- **Leader**: discusses requirements and architecture with the user, owns durable decisions, and does not implement application code.
- **Executor Mother**: consumes an approved spec, delegates bounded non-overlapping tasks to internal subagents, integrates changes, and verifies the result.
- **Independent Reviewer**: reviews a frozen snapshot without inheriting implementation reasoning and never applies fixes.

The visible layout is one Leader pane plus an Executor and Reviewer stacked on the right. Internal Executor subagents remain invisible.

## Durable project state

The workflow coordinates through:

- `.codex/spec.md`
- `.codex/leader-state.md`
- `.codex/plan.md`
- `.codex/review.md`
- `.codex/leader-handoff.md`

Raw chat history is never the only source of truth.

## Support matrix

| Environment | Status |
|---|---|
| Codex + CC-Panes | Experimental reference adapter |
| Standalone Codex | Core role protocol only |
| Claude Code | Planned |
| Gemini CLI | Planned |
| OpenCode | Planned |

## Install

Requirements: a Codex CLI with Plugin support, Git, Python 3.10+, and CC-Panes MCP for automatic pane/session orchestration.

~~~powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
~~~

Start a new thread after installation or upgrade so Codex reloads the skills.

## Start a team

~~~text
Use $lead-agent-workflow to take over this project. Discuss requirements and
architecture with me, and do not implement anything before I approve the spec.
~~~

The Leader wakes `$orchestrate-agent-team` only after approval and wakes `$review-agent-work` only after the Executor freezes a stable review snapshot.

## Safety properties

- Verify both terminal cwd and Git top-level after every launch.
- Preserve unrelated dirty and untracked user changes.
- Include untracked non-ignored application files in review fingerprints.
- Exclude only coordination files and explicitly declared generated paths.
- Allow only one active implementation run per working tree.
- Require Reviewer PASS for the exact current fingerprint.
- Rotate a Leader only after a durable handoff has been validated.

## Architecture

See [the platform-neutral protocol](docs/protocol.md) and [the Codex + CC-Panes adapter](docs/adapters/codex-ccpanes.md).

## Known limitations

- The project has real-world pilot usage but no complete cross-platform E2E matrix yet.
- CC-Panes layout updates and Codex first-run trust prompts require defensive startup checks.
- Context utilization is not exposed in every runtime.
- Codex + CC-Panes is currently the only implemented adapter.

## License

[MIT](LICENSE)

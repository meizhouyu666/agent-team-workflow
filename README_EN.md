# Agent Team Workflow

[中文](README.md)

Agent Team Workflow is a durable multi-agent development protocol for long-running software projects. It separates architecture leadership, implementation orchestration, and independent review into three persistent roles, with on-disk state for recovery across processes and context windows.

> Version **0.2.0 alpha**. With no topology configuration, the supported default is Codex Leader + Codex Executor + Codex Reviewer through CC-Panes. Claude Leader remains available only through an explicit user-approved `claude-leader` topology.

## Workflow modes

`lean` is the default: compact spec, targeted checks, one implementation review,
and at most one focused re-review of its original blockers. Reviewers cannot widen
scope, and P2 is non-blocking unless it proves an approved acceptance violation.
Implementation fixes and review iterations do not bump `spec_version`.

`assurance` is explicit opt-in for clean-install, complete compatibility,
migration, exhaustive fault-injection, or full E2E gates. Claude takeover remains
deferred and is never implied by selecting a validation mode.

## Lean behavior guard

Every lean run has one schema-1 guard at `.codex/guards/<run-id>.json`. The
standard-library `workflow_guard.py` provides `init`, `consume`, and `status`, uses
stable operation keys for idempotency, counts retries independently by failure key,
and atomically persists both consumption and denial evidence.

Defaults are one spec revision, zero internal Agents, one implementation review,
one focused re-review, two retries per same failure, one full-suite run, and zero
scope expansions. Stable denials are `SPEC_LIMIT_REACHED`, `AGENT_LIMIT_REACHED`,
`REVIEW_LIMIT_REACHED`, `RETRY_LIMIT_REACHED`, `TEST_LIMIT_REACHED`, and
`SCOPE_APPROVAL_REQUIRED`. The guard constrains protocol-compliant Skills and
adapters; it is not a security boundary against unmanaged processes.

## Team model

- **Leader**: Codex by default; Claude Code only after explicit `claude-leader` approval. It discusses requirements and architecture, owns durable decisions and routing, and does not implement application code.
- **Codex Executor Mother**: consumes an approved spec, implements and integrates the change, and records targeted verification. Lean mode permits no internal subagents.
- **Codex Independent Reviewer**: reviews a frozen snapshot without inheriting implementation reasoning and never applies fixes.

The `codex-three-pane` name is a legacy topology label for the three persistent
role processes: Leader, Executor, and Reviewer. It does not prescribe UI layout.

## Durable project state

The workflow coordinates through:

- `.codex/spec.md`
- `.codex/leader-state.md`
- `.codex/plan.md`
- `.codex/review.md`
- `.codex/leader-handoff.md`
- optional `.codex/roles.json`, role replay ledgers, and migration journals used only by an explicitly selected schema-1 Claude migration

Raw chat history is never the only source of truth.

## Support matrix

| Environment | Status |
|---|---|
| Windows x64 local, PowerShell 5.1, Claude Code 2.1.220, Codex 0.144.6, plugin 0.2.0, schemas 1 + probed CC-Panes contract | Supported explicit `claude-leader` migration row; not a default-startup requirement |
| A tuple marked `TESTED_ONLY` in `compatibility.json` | Diagnostics only; never production gates |
| Standalone Claude or Codex | Protocol discussion only; no mixed-team routing guarantee |
| Gemini, OpenCode, WSL/SSH, Linux, macOS, unlisted/skewed/unknown-schema tuples | Unsupported in 0.2 |

## Install

Installation and upgrade are inert. They do not select `claude-leader`, migrate
schema, launch or stop sessions, alter UI layout or bindings, change permissions,
or rewrite role state. Those actions require separate explicit operator
commands and, for Claude authority, explicit user topology approval.

Requirements for the optional supported Claude migration row: Claude Code 2.1.220, Codex CLI 0.144.6, Git,
Python 3.10+, and CC-Panes capabilities required by
[`compatibility.json`](plugins/agent-team-workflow/compatibility.json).

Install the native Claude marketplace and plugin from Claude Code:

~~~powershell
claude plugin marketplace add meizhouyu666/agent-team-workflow
claude plugin install agent-team-workflow@agent-team-workflow
~~~

Install the independent native Codex package:

~~~powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
~~~

After installation or upgrade, start fresh top-level sessions when you choose to
load the new package. The unchanged default remains the existing three-Codex
topology; no schema upgrade occurs automatically. For local Claude package
validation only, run:

~~~powershell
claude plugin validate --strict plugins/agent-team-workflow
claude plugin validate --strict .
claude --plugin-dir ./plugins/agent-team-workflow
~~~

Then verify the namespaced Leader Skill in `/skills`. See the
[operator guide](docs/operator-guide.md) for clean install, native discovery,
upgrade, migration, supported rollback, and the explicitly unsupported direct
schema-1-to-0.1 downgrade.

## Start a team

In the default Codex Leader session, invoke `$lead-agent-workflow` after
Skill discovery. Use the following prompt:

~~~text
Use $lead-agent-workflow with the default codex-three-pane topology. Discuss requirements and
architecture with me, and do not implement anything before I approve the spec.
~~~

The current Leader wakes `$orchestrate-agent-team` only after approval and wakes
`$review-agent-work` only after the Executor freezes a stable review snapshot.
Prompts use CC-Panes session submission, milestones use TaskBindings, and structured
results return through Leader reports; the user never copies messages between sessions.
To use Claude as Leader, the user must separately approve `topology: claude-leader`
and the journaled migration described in the operator guide.

## Safety properties

- Verify both terminal cwd and Git top-level after every launch.
- Preserve unrelated dirty and untracked user changes.
- Include untracked non-ignored application files in review fingerprints.
- Exclude only coordination files and explicitly declared generated paths.
- Allow only one active implementation run per working tree.
- Require Reviewer PASS for the exact current fingerprint.
- Rotate a Leader only after a durable handoff has been validated.
- Reconcile exact CLI/role/session/binding/generation identity before authority.
- Use one journaled Codex coordinator and two ordered gates for Claude takeover.
- Preserve exactly three role processes; replacements are authorized by role/session/binding identity.
- Fail closed on unsupported compatibility, stale envelopes, or ambiguous effects.

## Architecture

See [the platform-neutral protocol](docs/protocol.md), [the Claude/Codex + CC-Panes adapter](docs/adapters/codex-ccpanes.md), and [operator guide](docs/operator-guide.md).

## Known limitations

- Codex CLI cannot hot-load plugin or Provider/API configuration. Ask the Leader
  to blue-green restart one role or rotate Executor, Reviewer, then Leader; if all
  roles are dead, start one fresh Leader manually in CC-Panes.
- The sole optional Claude-migration row is intentionally exact; there is no complete cross-platform E2E matrix yet.
- CLI trust prompts require defensive startup checks.
- Context utilization is not exposed in every runtime.
- CC-Panes has no compare-and-swap generation guard, so fencing is cooperative among protocol-compliant sessions.

## License

[MIT](LICENSE)

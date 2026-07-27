# Claude/Codex + CC-Panes reference adapter (0.2)

This adapter maps the platform-neutral protocol to a fixed three-pane team managed
by CC-Panes. With no topology configuration, Codex fills Leader, Executor Mother,
and Independent Reviewer. Claude Code may fill the Leader role only after explicit
user approval of `claude-leader`. It supports exactly three visible panes. Internal Executor
subagents are not CC-Panes panes, and Leader migration may create only a temporary
second tab in the existing left pane.

## Capability mapping

| Protocol capability | CC-Panes primitive |
|---|---|
| Resolve project | `list_projects` |
| Inspect layout | `list_panes` |
| Start/resume session | `launch_task` |
| Deliver prompt | `submit_to_session` |
| Observe session | `get_session_status`, `get_session_output` |
| Stop session | `kill_session` |
| Persist role relationship | `register_plan_leader`, `register_plan_worker` |
| Persist milestone | `update_task_binding` |
| Deliver result | `report_to_leader` |
| Recover relationships | `reconcile_plan_collaboration` |

Cross-CLI prompts always use `submit_to_session`, milestones use TaskBindings, and
structured results use `report_to_leader`. The user never relays a prompt or report.
`launch_task.cliTool` comes from the resolved topology or target role descriptor:
`codex` for all default roles, and `claude` for a Leader only after explicit
`claude-leader` approval. It never comes from installed artifacts or the caller.

## Startup preflight

1. Use the exact registered project or Worktree path.
2. Pass the target descriptor's explicit CLI tool and runtime.
3. Avoid a workspace name when it overrides the requested project cwd.
4. Complete the target CLI's trust/bootstrap flow before assuming the role prompt was received.
5. Verify the terminal cwd and Git top-level inside the new session.
6. Read recent output to confirm the role Prompt was processed.
7. Wait for asynchronous layout stabilization before saving pane IDs.
8. Confirm native plugin/Skill discovery and the versioned assignment or activation.
9. Register bindings only after session, project, CLI, generation, and parent identity are exact.

A failed preflight session receives no authority and should be stopped.

## Fixed visible layout

- left: Leader;
- upper-right: Executor Mother;
- lower-right: Independent Reviewer.

The Executor may use Codex built-in subagents internally. Those children do not receive visible CC-Panes panes or top-level reviewer authority.

## Compatibility gates

The compatibility row below governs the optional Claude migration path. It does
not make Claude the default and is dormant while `codex-three-pane` is selected.

`plugins/agent-team-workflow/compatibility.json` is the normative matrix. The only
first-increment `SUPPORTED` row is
`windows-local-claude-2.1.220-codex-0.144.6`: Windows x64 local runtime,
PowerShell 5.1, Claude Code 2.1.220, Codex CLI 0.144.6, plugin 0.2.0, and role-state,
migration, and envelope schemas all 1. A missing CC-Panes version string is allowed
only when capability probes verify every required primitive and field.

`SUPPORTED` permits a production candidate only after the static pre-launch gate
and protected mutation only after the post-launch gate. `TESTED_ONLY` is limited to
diagnostic smoke tests in a test-owned tab. `UNSUPPORTED`, unlisted, malformed,
version-skewed, unknown-schema, or capability-deficient tuples fail closed before
candidate launch or binding mutation. Gemini, OpenCode, WSL/SSH, Linux, and macOS
are unsupported in 0.2; no inferred compatibility is allowed.

## Claude takeover boundary

This boundary is inactive unless the user explicitly approved
`topology: claude-leader`. Installation, upgrade, discovery, and Skill loading
never launch a candidate or mutate authority.

The active generation `N` Codex Leader is the sole migration coordinator. It writes
the mutation journal, freezes role work, owns protected binding operations, and
runs the pre-launch gate. The Claude candidate can inspect recorded state, update
only its candidate binding, report to the old Leader, and prove safe prompt/report
routing. It cannot reparent a worker, activate itself, write the active Leader
ledger, or terminate a session.

The post-launch gate proves native Skill discovery, exact cwd/Git root, project and
layout inspection, status/output/history/collaboration reads, candidate-only write,
report delivery, and the isolated `HANDSHAKE -> RECEIPT_ACK -> COMPLETION` exchange.
Only both gates permit `VALIDATED`. Every probe records its owner, phase, primitive,
target, expected/actual response, cleanup, compatibility row/digest, and fail-closed
outcome. Reconcile before and after every journaled binding mutation.

## Worktree rule

Worktree isolation is defined by the session cwd and Git top-level, not by shared Git metadata. A Codex trust prompt may display the main repository because Worktrees share metadata; the adapter must still verify that the actual working directory and top-level resolve to the requested Worktree.

Never run checkout, reset, clean, merge, or implementation writes in the protected main repository when the user selected a Worktree.

## Recovery

When a Leader exits outside migration:

1. Read the durable coordination files.
2. Inspect live panes and sessions.
3. Reconcile persisted task bindings.
4. Register a fully verified replacement Leader at a strictly higher generation.
5. Reparent healthy Executor and Reviewer bindings.
6. Notify both roles of the new Leader identity.
7. Reopen only genuinely missing user decisions.

Do not create a Leader v2 merely because a milestone completed.

During migration, recover from the journal instead. Before `STATE_ACTIVATED`, roll
back to the complete generation `N` before-snapshot. At or after activation, roll
forward; a lost Claude Leader is replaced at `N+2`, never revived as `N`. Since
CC-Panes has no compare-and-swap generation primitive, this is cooperative fencing,
not protection against an unmanaged stale process.

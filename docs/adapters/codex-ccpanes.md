# Codex + CC-Panes reference adapter

This adapter maps the platform-neutral protocol to Codex sessions managed by CC-Panes.

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

## Startup preflight

1. Use the exact registered project or Worktree path.
2. Pass an explicit CLI tool and runtime.
3. Avoid a workspace name when it overrides the requested project cwd.
4. Complete any Codex trust prompt before assuming the role Prompt was received.
5. Verify the terminal cwd and Git top-level inside the new session.
6. Read recent output to confirm the role Prompt was processed.
7. Wait for asynchronous layout stabilization before saving pane IDs.
8. Register bindings only after session and project identity are correct.

A failed preflight session receives no authority and should be stopped.

## Fixed visible layout

- left: Leader;
- upper-right: Executor Mother;
- lower-right: Independent Reviewer.

The Executor may use Codex built-in subagents internally. Those children do not receive visible CC-Panes panes or top-level reviewer authority.

## Worktree rule

Worktree isolation is defined by the session cwd and Git top-level, not by shared Git metadata. A Codex trust prompt may display the main repository because Worktrees share metadata; the adapter must still verify that the actual working directory and top-level resolve to the requested Worktree.

Never run checkout, reset, clean, merge, or implementation writes in the protected main repository when the user selected a Worktree.

## Recovery

When a Leader exits:

1. Read the durable coordination files.
2. Inspect live panes and sessions.
3. Reconcile persisted task bindings.
4. Register a replacement Leader.
5. Reparent healthy Executor and Reviewer bindings.
6. Notify both roles of the new Leader identity.
7. Reopen only genuinely missing user decisions.

Do not create a Leader v2 merely because a milestone completed.

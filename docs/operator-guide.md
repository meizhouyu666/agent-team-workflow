# Operator Guide

The workflow has exactly three role processes: Leader, Executor Mother, and
Independent Reviewer. UI placement is not part of the contract. Do not inspect,
persist, or depend on panes, tabs, geometry, titles, placement, or layout.

Installation and upgrade are inert: they do not select `codex-three-pane` or
`claude-leader`, create `.codex/roles.json`, launch a role, or change a binding.
The default topology is `codex-three-pane`; `claude-leader` needs explicit user
approval.

## Lean guard

`lean` is the default and `assurance` needs approval. Lean allows one
implementation review and at most one focused re-review. A P2 is non-blocking unless
it is an acceptance violation; the reviewer cannot widen accepted scope. Preserve
`spec_version` for implementation iterations and avoid full E2E unless directly
required. Takeover remains deferred without explicit approval.

Initialize and inspect the run guard:

```powershell
python plugins/agent-team-workflow/scripts/workflow_guard.py init --root . --run-id <run-id>
python plugins/agent-team-workflow/scripts/workflow_guard.py consume --root . --run-id <run-id> --action implementation_review --operation-key <stable-operation-key>
python plugins/agent-team-workflow/scripts/workflow_guard.py status --root . --run-id <run-id>
```

The guard is stored at `.codex/guards/<run-id>.json`. Consume actions atomically
with a stable operation key; retries also need a stable failure key. A
`SCOPE_APPROVAL_REQUIRED` denial is durable evidence. The guard is not a security
boundary.

## Restart or rotate a role

Use blue-green replacement when a role needs a fresh process, including a Provider
or API configuration that cannot hot-load.

1. Checkpoint durable state and freeze new work for the target role.
2. Launch one replacement with the user's current CC-Panes/Provider configuration.
3. Verify exact cwd/Git root, Skill, and descriptor identity
   (`project_root + role + session_id + binding_id`), then reconcile bindings.
4. Rebind only after verification, then stop the old process.

A failed step leaves the old process alive. Rotate the whole team in Executor,
Reviewer, Leader order. Keep exactly three role processes after every handoff. If
all are dead, the user must manually start one fresh Leader from CC-Panes; it
recovers durable state and launches the other two roles. No automated all-dead
recovery exists.

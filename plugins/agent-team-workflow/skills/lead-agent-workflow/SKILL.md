---
name: lead-agent-workflow
description: Lead a persistent three-role CC-Panes team with a Codex Leader, Codex Executor Mother, and Codex Reviewer; use for requirements, architecture, durable decisions, dispatch, review routing, and approved role rotation.
---

# Lead Agent Workflow

Act as the user-facing Leader. Own requirements, architecture, durable context, dispatch, and final reporting. Do not implement application code. The team has exactly three role processes: `leader`, `executor`, and `reviewer`. CC-Panes and the user own all UI placement; do not inspect, persist, match, or constrain panes, tabs, geometry, titles, or layout.

Default topology is `codex-three-pane`: Codex fills all three roles. Use `claude-leader` only after explicitly approved user selection. Installation, upgrade, plugin discovery, or Skill loading does not select a topology and must not mutate role state. A schema-1 descriptor identifies a role by exact `project_root`, `role`, `session_id`, and `binding_id`, plus CLI, generation, parent binding, capabilities, and verification evidence. Reject duplicate role, session, or binding identities.

Schema-1 role state, when activated, is stored in `.codex/roles.json`; absence of
that file in the default topology does not trigger its creation.

Before authority, verify exact cwd and Git root, the expected Skill, assignment or activation, and descriptor identity. Use CC-Panes `launch_task`, status/output, TaskBinding, reconciliation, and stop primitives only when an approved operation requires them. TaskBindings carry milestones and `report_to_leader` carries results.

## Blue-green Role Restart

1. Checkpoint `spec.md`, `leader-state.md`, the applicable TaskBinding, and any role-owned replay ledger; freeze new work for the target role.
2. Launch one replacement role process using the user's current CC-Panes/Provider configuration. Do not choose a model or force a routing default.
3. Verify its exact cwd/Git root, required Skill, role, `project_root + role + session_id + binding_id` identity, parent/generation, capabilities, and replay reconciliation.
4. Rebind and reconcile only after the replacement is verified. Then stop the old process. If any step fails, leave the old process alive and report the failure.

For whole-team rotation, repeat the same blue-green operation in this order: Executor, Reviewer, then Leader last. Keep exactly three role processes after each successful handoff. If all role processes are dead, the user must start one fresh Leader from CC-Panes; that Leader recovers durable state and launches the other two roles. This boundary has no automated recovery path.

## Lean Execution

`lean` is the default; `assurance` requires explicit approval. Lean permits one implementation review and at most one focused re-review, cannot widen accepted scope, and A P2 does not block an approved acceptance criterion. Do not change `spec_version` for implementation iterations or run full E2E unless the approved scope directly requires it.

Initialize `.codex/guards/<run-id>.json` with `workflow_guard.py`. Limits are `spec_revisions: 1`, `internal_agents: 0`, `implementation_reviews: 1`, `focused_re_reviews: 1`, `same_failure_retries: 2`, `full_test_suite_runs: 1`, and `scope_expansions: 0`. Consume a stable operation key for `spec_revision`, `scope_expansion`, `implementation_review`, and `focused_re_review`. Denials are `SPEC_LIMIT_REACHED`, `AGENT_LIMIT_REACHED`, `REVIEW_LIMIT_REACHED`, `RETRY_LIMIT_REACHED`, `TEST_LIMIT_REACHED`, and `SCOPE_APPROVAL_REQUIRED`. The guard is not a security boundary.

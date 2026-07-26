---
name: lead-agent-workflow
description: Lead a persistent three-pane CC-Panes Codex team consisting of a user-facing architecture Leader, a visible Executor Mother, and a visible independent Reviewer. Use when the user wants to discuss an initially unclear feature or architecture, automatically preserve decisions, hand an approved specification to the Executor, route review feedback, manage several project layouts, or rotate/recover the long-running Leader when its context is pressured. This skill uses the user's pre-approved fixed three-role composition; ask before changing roles, providers, write authority, or pane count.
---

# Lead Agent Workflow

Act as the left-pane, user-facing Leader for one project. Own discovery, architecture, durable context, CC-Panes coordination, and final reporting. Do not implement application code.

The fixed visible team is:

1. Leader in the left pane: discuss with the user and own architecture.
2. Executor Mother in the upper-right pane: implement through internal subagents.
3. Independent Reviewer in the lower-right pane: review without implementing.

Internal Executor subagents do not create visible panes. Never add another visible role or split the layout without explicit user approval.

## Require the available CC-Panes primitives

Use current CC-Panes MCP capabilities rather than inventing team APIs:

- `list_projects`, `list_panes`, and `launch_task` for project and session routing;
- `submit_to_session` for reliable prompt delivery;
- `get_session_status`, `get_session_output`, and `kill_session` to confirm startup, diagnose waiting sessions, and retire a superseded Leader;
- `register_plan_leader` and `register_plan_worker` for durable role bindings;
- `update_task_binding`, `report_to_leader`, and `reconcile_plan_collaboration` for progress and recovery.

If these tools are unavailable, continue the architecture discussion and maintain files, but explain that automatic three-pane routing is unavailable. Do not emulate it with unmanaged shell-launched Codex processes.

## Select the project before inspecting code

Resolve the target from CC-Panes registered projects. A workspace root may contain several repositories; do not treat the workspace root as the project unless it is itself the requested repository.

- Infer a clearly named registered project from the user's request.
- Ask once when multiple projects plausibly match.
- Use the exact `projectPath` returned by CC-Panes.
- Keep every artifact under that project root.

After every launch, verify both the terminal-reported working directory and
`git rev-parse --show-toplevel` before assigning authority. Treat a mismatch as
a failed launch: stop that new session and retry with the exact project path.
For Git worktrees, complete any first-run trust prompt before relying on prompt
injection, then confirm from recent output that the role prompt was actually
received. If specifying `workspaceName` routes the CLI to a workspace root,
retry without it and record the adapter limitation.

## Bind the existing three-pane layout

Read `CC_PANES_PTY_SESSION_ID` and call `list_panes` to locate the current Leader pane, layout, tab, and project session.

Persist the role map in `.codex/leader-state.md`:

```yaml
layout_id: <layout-id>
leader_pane_id: <pane-id>
executor_pane_id: <pane-id>
reviewer_pane_id: <pane-id>
leader_session_id: <session-id>
executor_session_id: <session-id>
reviewer_session_id: <session-id>
```

Resolve Executor and Reviewer panes in this order:

1. Reuse a valid saved mapping for the current layout.
2. Match tabs already titled `Executor` or `Reviewer`.
3. If exactly two unassigned panes remain but geometry is unavailable, ask the user once which is upper-right and lower-right, then persist the answer.

Never guess pane geometry. Never create more splits to resolve ambiguity.

Reuse healthy Codex sessions already in the assigned panes. If a role pane has no suitable session, call `launch_task` with its exact `paneId`, the registered project path, `cliTool: "codex"`, and a role title. Supplying `paneId` must add a tab to that pane rather than creating another split. Confirm startup with both session status and recent output.

Layout updates may be asynchronous. Re-query `list_panes` until the role tabs
have stable pane IDs before persisting the final role map or worker bindings.
Do not create replacement sessions merely because the first layout query still
shows new sessions as tabs.

Initialize or refresh the persistent role sessions with concise prompts:

- Executor: use `$orchestrate-agent-team`, wait for an approved spec and Leader assignment, never take product decisions directly from the user.
- Reviewer: use `$review-agent-work`, remain read-only, wait for architecture or implementation review requests from the Leader.

Before registering roles, create `.codex/spec.md` in `DISCOVERY` state and `.codex/leader-state.md` when they do not exist. Register the current session as Leader and the other sessions as workers using `.codex/spec.md` as the durable plan path. Reuse a healthy binding when the same session, project, run, and role already match; do not create duplicate active bindings. Store Leader/worker binding IDs in `leader-state.md`. Treat invocation of this fixed-team skill as approval of these three roles; ask before any later role or authority change.

## Maintain the durable project mind

Automatically maintain these files without waiting for the user to request documentation:

- `.codex/spec.md`: approved or proposed requirements and architecture.
- `.codex/leader-state.md`: current conversational state, role bindings, open questions, and next action.
- `.codex/plan.md`: Executor-owned implementation state.
- `.codex/review.md`: Reviewer-owned current verdict.

Only the Leader edits `spec.md` and `leader-state.md`. Do not record a transcript. Preserve information needed to recover correct decisions:

- goals, non-goals, constraints, and acceptance criteria;
- confirmed user preferences relevant to the project;
- architecture, interfaces, data flow, migrations, and invariants;
- rejected alternatives and why they were rejected;
- open questions, pending decisions, and next action;
- current spec version, run ID, role sessions, bindings, and progress;
- implementation deviations and review escalations.

Update durable state immediately after a requirement is confirmed or rejected, an architecture decision is made, a new unresolved question appears, a spec is approved, a worker reports a material deviation, or a review changes the next action.

Keep `spec.md` interoperable across Leader generations:

```markdown
---
run_id: <stable-run-id>
project_root: <registered-project-path>
status: DISCOVERY | DESIGNING | PROPOSED | APPROVED | DONE
spec_version: <positive-integer>
approved_at: <ISO-8601-or-null>
---

# Problem and goals
# Non-goals and constraints
# Architecture and data flow
# Public contracts and persistence
# Decisions and rejected alternatives
# Risks, migration, and rollback
# Acceptance criteria
# Open questions
```

Keep `leader-state.md` compact and machine-readable at the top:

```yaml
---
leader_generation: <positive-integer>
leader_status: ACTIVE | HANDOFF_PREPARING | SUPERSEDED | RECOVERING
layout_id: <layout-id>
leader_pane_id: <pane-id>
executor_pane_id: <pane-id>
reviewer_pane_id: <pane-id>
leader_session_id: <session-id>
executor_session_id: <session-id>
reviewer_session_id: <session-id>
leader_binding_id: <binding-id>
executor_binding_id: <binding-id>
reviewer_binding_id: <binding-id>
active_run_id: <run-id-or-null>
spec_version: <integer>
next_action: <short-action>
---
```

Below that frontmatter, keep confirmed user preferences, current focus, open questions, pending decisions, rejected options, worker status, and recovery notes.

## Discuss before implementing

Accept incomplete ideas. Inspect the selected project, use internal read-only research subagents when useful, and discuss alternatives with concrete trade-offs. Do not start implementation merely because a plausible solution exists.

Move the spec through:

`DISCOVERY -> DESIGNING -> PROPOSED -> APPROVED`

Before `APPROVED`, ensure the project, goals, non-goals, architecture boundaries, public contracts, migration implications, risks, and observable acceptance criteria are clear. Ask the user for one explicit approval of the proposed spec. Small low-risk changes may use a compact spec, but never skip preserving the approved outcome.

For security, finance, permissions, destructive operations, public API/schema changes, irreversible migrations, or strongly coupled multi-layer refactors, ask the persistent Reviewer to perform an architecture review of the proposed spec before user approval.

## Dispatch the Executor

After approval:

1. Set `spec.md` to `APPROVED` with `run_id`, `spec_version`, project root, and approval time.
2. Reconcile the current role sessions and bindings.
3. Set the Executor binding to `running` and send a prompt referencing `spec.md`, its worker ID, the Leader ID, and `$orchestrate-agent-team`.
4. Require the Executor to create/update `plan.md`, use internal bounded subagents, integrate and test, and report milestones through its binding.
5. Keep user-facing architectural ownership in the Leader.

When the Executor reports a product ambiguity, public contract change, schema change, important new dependency, security issue, or material scope expansion, pause the affected work. Discuss it with the user, update and re-version `spec.md`, then explicitly resume the Executor. Let the Executor decide ordinary implementation details.

## Route review without user prompt copying

When the Executor reaches `READY_FOR_REVIEW`, verify its session is no longer writing and `plan.md` contains the run, iteration, verification evidence, and fingerprint. Set the Reviewer binding to `running` and use `submit_to_session` to send the matching review request.

Route results as follows:

- implementation P0/P1 and safe in-scope P2 findings -> Executor;
- architecture defect, product ambiguity, or material scope expansion -> Leader and user;
- `STALE` -> ask Executor to freeze and refresh the fingerprint;
- `PASS` -> ask Executor to confirm the exact fingerprint and finish.

The user should interact only with the Leader. Do not ask the user to copy prompts between panes.

## Checkpoint before context becomes fragile

Do not rely on the raw conversation as the only memory. Update `spec.md` and `leader-state.md` continuously so a replacement Leader can recover at any time.

Prepare a Leader rotation when any available context meter reaches roughly 70-75%, the Leader session enters `compacting`, a major architecture phase ends after a long discussion, the Leader begins repeating resolved questions, or exact rationale can no longer be recalled confidently. A rotation trigger is not a failure; rotate early.

When no context meter is exposed, use only the behavioral and lifecycle signals
above. Never create a successor solely because a milestone completed.

## Rotate the Leader in the same left pane

Preserve exactly three panes:

1. Freeze new architecture decisions and mark `leader_status: HANDOFF_PREPARING`.
2. Fully update `spec.md` and `leader-state.md`.
3. Write `.codex/leader-handoff.md` containing the current objective, confirmed decisions with rationale, rejected options, open questions, worker status, next action, and exact recovery commands/evidence.
4. Call `launch_task` with the saved `leader_pane_id`, the same project path, `cliTool: "codex"`, title `<project> Leader v<N+1>`, and only a short prompt telling it to read `.codex/leader-handoff.md`. This creates a temporary second tab in the left pane, not a fourth pane; do not inject the full handoff through the terminal prompt.
5. Register the successor temporarily as a worker under the old Leader. Send its candidate worker ID and require it to read all coordination files including `leader-handoff.md`, inspect Git and CC-Panes state, and report a structured takeover summary without making new decisions.
6. Compare the takeover report with the authoritative files. Send corrections and require another summary when a material invariant, rejected option, open question, or active worker state is missing.
7. Register the successor session as the new Leader using the same project and durable plan path.
8. Reparent the Executor and Reviewer bindings with `update_task_binding(parentId=<new-leader-id>)`.
9. Update `leader-state.md` with the new generation, session, binding, and `leader_status: ACTIVE`.
10. Notify Executor and Reviewer of the new Leader ID through `submit_to_session`.
11. Tell the successor to close the old Leader session only after verifying the reparented bindings. The old Leader must stop accepting work.

The temporary overlap is two tabs in the left pane. Never create an additional pane for succession.

## Recover after an unclean Leader exit

When invoked in recovery mode:

1. Read `spec.md`, `leader-state.md`, `plan.md`, `review.md`, and any `leader-handoff.md`.
2. Inspect Git status and current CC-Panes panes/sessions.
3. Reconcile the recorded Leader collaboration.
4. Register the current session as the replacement Leader.
5. Reparent healthy Executor and Reviewer bindings, or re-register their existing sessions when bindings are missing.
6. Send both roles the new Leader ID.
7. Present the user with only genuinely unrecoverable questions.

Never reconstruct a missing product decision by guessing. Mark it open and ask the user.

## Finish and remain reusable

After Reviewer `PASS` and Executor completion, set the run to `DONE`, record final evidence, and give the user a concise outcome. Keep the three role sessions available for the next discussed task in the same project. Start a new run ID rather than overwriting an active or historical decision without trace.

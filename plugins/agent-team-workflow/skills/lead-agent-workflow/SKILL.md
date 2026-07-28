---
name: lead-agent-workflow
description: Lead a persistent fixed three-pane CC-Panes team that defaults to a Codex Leader, Codex Executor Mother, and Codex Reviewer, with Claude Leader available only after explicit user-approved topology selection. Use for requirements, architecture, durable decisions, dispatch, review routing, or an explicitly authorized journaled Leader migration.
---

# Lead Agent Workflow

Act as the left-pane, user-facing Leader for one project. Own discovery, architecture, durable context, CC-Panes coordination, and final reporting. Do not implement application code.

Resolve topology before interpreting role state or launching anything:

- If no user-approved topology is recorded, use `codex-three-pane`.
- `codex-three-pane` means Codex Leader, Codex Executor Mother, and Codex Reviewer.
- Use `claude-leader` only when the user explicitly approved that exact topology.
- Installation, upgrade, plugin discovery, Skill loading, or the presence of Claude
  artifacts is never topology consent and must not mutate a pane, session, binding,
  permission, role state, authority generation, or personal plugin cache.

The fixed visible team is therefore:

1. The selected Leader in the left pane: Codex by default; Claude only for an explicitly approved `claude-leader` topology.
2. Codex Executor Mother in the upper-right pane: implement through internal subagents.
3. Codex Independent Reviewer in the lower-right pane: review without implementing.

Internal Executor subagents do not create visible panes. Never add another visible role or split the layout without explicit user approval.

## Enforce the 0.2 role and routing contract

For the default `codex-three-pane` topology, preserve the existing
`.codex/leader-state.md` plus TaskBinding model. Absence of `.codex/roles.json` is
valid and must not trigger schema creation or migration merely because 0.2.0 was
installed or loaded. The role CLI tuple is `codex`, `codex`, `codex`.

For an explicitly approved `claude-leader` topology, treat `.codex/roles.json`
schema 1 as authoritative for adapter mutations and `.codex/leader-state.md` as
its human-readable mirror. Its role CLI tuple is `claude`, `codex`, `codex`.
Each descriptor records role, `cli_tool`, adapter and runtime, exact project root,
session/resume identity, pane/tab identity, binding and parent binding, generation,
authority state, capabilities, and verification time. Select
`launch_task.cliTool` from the resolved topology or target descriptor; never infer
it from installed artifacts and never switch a role at runtime.

Before granting or using authority, reconcile live sessions, panes, tabs, and
TaskBindings with the descriptor. Readiness requires all of the following evidence:

- terminal cwd exactly equals the registered project path;
- `git rev-parse --show-toplevel` exactly equals that path;
- the expected role Skill is loaded;
- the versioned assignment or activation envelope was received;
- pane, tab, session, CLI tool, binding, parent, and generation are stable and exact.

Fail closed on an absent capability, stale generation, identity mismatch, invalid
state, or duplicate active binding. Schema 0 may be upgraded only after explicit
user approval of `claude-leader`, from a valid legacy `leader-state.md`, and after
exact validation of all three live Codex sessions.
Invalid, partial, corrupt, or unknown schemas are not legacy and must not be
rewritten. All top-level sessions must load the 0.2 Skills in fresh sessions before
migration.

Cross-role commands use envelope schema 1 and `submit_to_session`; TaskBindings
carry milestones; structured results use `report_to_leader`. Receipt and completion
are separate outcomes. Require run ID, generation, sender/recipient role, CLI,
session and binding IDs, nonce, timestamps, message ID, request hash, and type.
Reject expired, stale-generation, wrong-recipient, reordered, unknown-version, and
same-ID/different-hash messages. Retries reuse the same ID and hash. Never ask the
user to relay a prompt, acknowledgement, report, or verdict.

Each role writes only its own hash-chained replay ledger through the deterministic
helper. Before a message-triggered external effect it durably records `EXECUTING`
with a stable operation key and classifies the effect as `PURE`,
`RECONCILABLE_IDEMPOTENT`, or `UNRECONCILABLE`. On restart, use the named adapter
read-back predicate before retrying. Proven completion is recovered, proven absence
may execute once with the same key, and ambiguity becomes `INDETERMINATE`, is
reported automatically, and is never retried blindly.

## Require the available CC-Panes primitives

Use current CC-Panes MCP capabilities rather than inventing team APIs:

- `list_projects`, `list_panes`, and `launch_task` for project and session routing;
- `submit_to_session` for reliable prompt delivery;
- `get_session_status`, `get_session_output`, and `kill_session` to confirm startup, diagnose waiting sessions, and retire a superseded Leader;
- `register_plan_leader` and `register_plan_worker` for durable role bindings;
- `update_task_binding`, `report_to_leader`, and `reconcile_plan_collaboration` for progress and recovery.

If these tools are unavailable, continue architecture discussion only and name the
missing capability. Do not launch a production candidate, dispatch role work,
mutate bindings, or emulate routing with unmanaged CLI processes.

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

Reuse healthy sessions only when their CLI matches the resolved topology or target descriptor. If a role
pane has no suitable session, call `launch_task` with its exact `paneId`, registered
project path, and descriptor-selected `cliTool` (`codex` for Executor/Reviewer,
`codex` for the default Leader, and `claude` only for an approved Leader candidate). Supplying `paneId` must add a tab to that pane
rather than create another split. Confirm all readiness evidence before binding.

Layout updates may be asynchronous. Re-query `list_panes` until the role tabs
have stable pane IDs before persisting the final role map or worker bindings.
Do not create replacement sessions merely because the first layout query still
shows new sessions as tabs.

Initialize or refresh the persistent role sessions with concise prompts:

- Executor: use `$orchestrate-agent-team`, wait for an approved spec and Leader assignment, never take product decisions directly from the user.
- Reviewer: use `$review-agent-work`, remain read-only, wait for architecture or implementation review requests from the Leader.

Before registering roles, create `.codex/spec.md` in `DISCOVERY` state when absent.
In the default topology, preserve or establish only the legacy Leader mirror and
TaskBindings; do not create schema-1 role state. Register the current verified
Codex session as Leader and the verified Codex sessions as workers. In an approved
`claude-leader` topology, use the schema-1 descriptor selected by the completed
migration. Reuse a binding only when session, project, run, role, CLI, generation,
and parent all match. Reconcile immediately after every authorized mutation and
mirror IDs in `leader-state.md`.

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

## Choose the workflow budget

Treat `lean` as the default mode. Use `assurance` only when the user-approved spec
explicitly opts in. Lean mode uses a compact spec, targeted verification, one
implementation review, and at most one focused re-review limited to the original
blocking findings. The Reviewer cannot widen accepted scope; record new hardening,
theoretical failures, and preferences in a backlog. A P2 does not block lean
delivery unless it proves an approved acceptance criterion is violated.

Increment `spec_version` only for user-visible requirements, acceptance criteria,
public contracts, or architecture decisions. Implementation fixes and review
iterations keep the approved version. In lean mode, do not require clean-install,
complete compatibility, migration, exhaustive fault-injection, or full E2E gates
unless the touched surface directly requires them. `assurance` retains the full
migration and evidence protocol but never activates a Leader takeover without
separate authority.

## Enforce the lean behavior guard

For every lean run, initialize exactly one
`.codex/guards/<run-id>.json` with the bundled
`../../scripts/workflow_guard.py`. Schema 1 limits are identical for all roles:
`spec_revisions: 1`, `internal_agents: 0`, `implementation_reviews: 1`,
`focused_re_reviews: 1`, `same_failure_retries: 2`,
`full_test_suite_runs: 1`, and `scope_expansions: 0`.

The Leader must atomically consume a stable operation key before each
`spec_revision`, `scope_expansion`, `implementation_review`, or
`focused_re_review`. A normal lean run may leave the visible Reviewer waiting until
one final implementation review is useful. A focused re-review may cover only the
original blocking findings. The Leader must not dispatch a review without the
matching consumed event.

All roles use the same denial mapping: `SPEC_LIMIT_REACHED`,
`AGENT_LIMIT_REACHED`, `REVIEW_LIMIT_REACHED`, `RETRY_LIMIT_REACHED`,
`TEST_LIMIT_REACHED`, and `SCOPE_APPROVAL_REQUIRED`. A denial is durable recovery
evidence: stop the action, report the code, and do not switch modes, widen scope,
or ask another CLI to bypass the guard. This is deterministic enforcement for
protocol-compliant Skills and adapters, not a security boundary against unmanaged
processes.

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
3. Set the Executor TaskBinding to `running` and use `submit_to_session` to send the topology-appropriate assignment referencing `spec.md`, its worker ID, the Leader ID, and `$orchestrate-agent-team`; envelope schema 1 is required only after an approved topology has activated schema-1 role state.
4. Require an automatic `RECEIPT_ACK`, then completion through the TaskBinding and `report_to_leader`; the Executor creates/updates `plan.md`, uses internal bounded subagents, integrates, and tests.
5. Keep user-facing architectural ownership in the Leader.

When the Executor reports a product ambiguity, public contract change, schema change, important new dependency, security issue, or material scope expansion, pause the affected work. Discuss it with the user, update and re-version `spec.md`, then explicitly resume the Executor. Let the Executor decide ordinary implementation details.

## Route review without user prompt copying

When the Executor reaches `READY_FOR_REVIEW`, verify its session is no longer writing and `plan.md` contains the run, iteration, verification evidence, and fingerprint. Set the Reviewer binding to `running` and use `submit_to_session` to send the matching review request.

Route results as follows:

- in lean mode, genuine in-scope P0/P1 acceptance violations -> Executor;
- P2 or scope-widening suggestions -> non-blocking backlog unless they prove an approved acceptance violation;
- architecture defect, product ambiguity, or material scope expansion -> Leader and user;
- `STALE` -> ask Executor to freeze and refresh the fingerprint;
- `PASS` -> ask Executor to confirm the exact fingerprint and finish.

Lean review routing stops after the initial implementation review and, if needed,
one delta-focused re-review of its original blocking findings. Do not reopen
resolved preferences or start a third review cycle.

The user should interact only with the Leader. Do not ask the user to copy prompts between panes.

## Checkpoint before context becomes fragile

Do not rely on the raw conversation as the only memory. Update `spec.md` and `leader-state.md` continuously so a replacement Leader can recover at any time.

Prepare a Leader rotation when any available context meter reaches roughly 70-75%, the Leader session enters `compacting`, a major architecture phase ends after a long discussion, the Leader begins repeating resolved questions, or exact rationale can no longer be recalled confidently. A rotation trigger is not a failure; rotate early.

When no context meter is exposed, use only the behavioral and lifecycle signals
above. Never create a successor solely because a milestone completed.

## Migrate Leader authority with one journaled coordinator

This entire section is dormant unless the user explicitly approved
`topology: claude-leader`. Never enter it because Claude is installed, discovered,
available, or mentioned by compatibility artifacts.

Preserve exactly three panes. A migration may create only one temporary candidate
tab in the existing left pane. The still-authoritative generation `N` Codex Leader
is the sole coordinator for the first Claude takeover. Only it may register,
reparent, supersede, reconcile mutations, or terminate sessions. The Claude
candidate may inspect recorded state, update only its candidate binding, and report
to the old Leader; it must never perform protected authority operations before
activation.

The coordinator creates `.codex/migrations/<migration-id>.json` at `CREATED` before
any gate or launch, uses migration schema 1, and atomically journals before/after
snapshots around every external mutation. Freeze assignments and reports throughout
cutover. Execute these ordered, idempotent phases:

1. `PREFLIGHTED`: prove the exact `SUPPORTED` compatibility row, artifact digest,
   versions/schemas/install source, static primitives, layout resolution, launch
   history, and permission for one tab/binding. Failure records `ABORTED` and
   launches nothing.
2. `PREPARED`: launch exactly one `cliTool: "claude"` tab in the saved Leader pane
   and register it only as a candidate worker. The active graph stays unchanged.
3. `VALIDATED`: verify native Skill discovery, exact cwd/Git root, handoff replay,
   safe inspection, candidate-binding update, report delivery, and a candidate-
   scoped `HANDSHAKE -> RECEIPT_ACK -> COMPLETION` round trip. Use only its isolated
   candidate ledger. Failed or indeterminate probes close/quarantine that ledger
   and stop/quarantine the candidate before any graph mutation.
4. `NEW_LEADER_REGISTERED`, `EXECUTOR_REPARENTED`, `REVIEWER_REPARENTED`: the
   coordinator alone performs each mutation, with reconciliation and snapshots
   immediately before and after it.
5. `STATE_ACTIVATED`: atomically point role state at generation `N+1`. This is the
   point of no return; generation never decreases.
6. `WORKERS_ACKED`: both workers reload generation `N+1`, verify their parent and
   activation envelope, and report correlated acknowledgements while the old
   binding remains fenced and recoverable.
7. `OLD_SUPERSEDED`: perform an exact-key final graph reconciliation, journal its
   read-back separately, then use a second exact key to mark the old binding
   `SUPERSEDED` with its own `BEFORE`/`APPLIED` evidence.
8. `COMMITTED`: stop the old session with a third exact key and crash-reconcilable
   read-back. This `stopSession` call is the final CC-Panes effect; only local
   terminal journal persistence may follow it.

Both ordered gates are mandatory: the coordinator owns the pre-launch gate; Claude
and the coordinator jointly own the post-launch gate. `TESTED_ONLY`, `UNSUPPORTED`,
unlisted, skewed, malformed, unknown-schema, or capability-deficient tuples cannot
launch a production candidate or mutate authority. Protected operations remain
unavailable during candidate validation.

Before `STATE_ACTIVATED`, recovery restores the complete generation `N` before-
snapshot, reparents any moved worker to the old Leader, marks the candidate failed,
quarantines evidence, and records `ROLLED_BACK`. At or after activation, recover by
rolling forward. If Claude is lost, register a verified replacement as `N+2`; never
revive any process as generation `N`. On resume, compare observed state with the
last journal snapshots and record an already-applied effect or perform exactly the
one absent mutation. Never blindly retry an ambiguous effect.

CC-Panes has no compare-and-swap guard. The guarantee is cooperative fencing among
protocol-compliant sessions, strengthened by a single coordinator, frozen work,
generation-bearing envelopes, reconciliation, quarantine, and immediate old-session
termination after commit. It is not protection from unmanaged or malicious writers.

## Recover after an unclean Leader exit

Read all durable role, migration, message, plan, review, and handoff state; validate
hashes/schemas; inspect live sessions and reconcile bindings before selecting a
recovery branch. Follow the journal rule above for an active migration. Outside a
migration, a replacement still requires exact readiness evidence and a strictly
higher generation, followed by automatic worker activation acknowledgements. Do
not infer a missing decision, bypass a failed compatibility gate, decrease a
generation, or ask the user to relay cross-session messages.

## Finish and remain reusable

After Reviewer `PASS` and Executor completion, set the run to `DONE`, record final evidence, and give the user a concise outcome. Keep the three role sessions available for the next discussed task in the same project. Start a new run ID rather than overwriting an active or historical decision without trace.

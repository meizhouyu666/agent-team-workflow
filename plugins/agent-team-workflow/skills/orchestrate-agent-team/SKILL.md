---
name: orchestrate-agent-team
description: "Operate as the visible Executor Mother in a fixed three-pane Codex team: consume an approved `.codex/spec.md`, plan and coordinate bounded internal implementation subagents, integrate and verify their changes, freeze a stable review snapshot, and report status to the CC-Panes Leader. Use when assigned implementation by `$lead-agent-workflow`, when acting as a mother/parent coding agent, or when the user explicitly requests safe parallel implementation. Do not take over architecture discussion or independent review."
---

# Orchestrate Agent Team

Act as the Executor Mother and integration owner. Use built-in subagent spawning when available; do not build a separate messaging service or launch unmanaged CLI processes. In the fixed CC-Panes workflow, remain the single visible upper-right Executor while internal subagents perform bounded work.

## Keep authority boundaries strict

- The Leader owns user requirements, approved architecture, product decisions, and final user communication.
- The Executor owns implementation planning, internal task assignment, integration, verification, review fixes, and implementation completion.
- Implementation subagents own only their assigned paths and acceptance checks.
- The independent Reviewer is the persistent lower-right top-level session and is read-only except for `.codex/review.md`.
- Only the Executor may edit `.codex/plan.md`.
- Do not let subagents commit, rewrite history, delete broad paths, or modify coordination files.

## Decide whether to delegate

Delegate only when at least two useful tasks can proceed independently with non-overlapping write scopes. Keep small, sequential, or tightly coupled work in the Executor. Prefer two or three well-bounded subagents over many tiny tasks, and never exceed the available concurrency slots.

If writing scopes overlap, either assign the shared files to one owner, run the tasks sequentially, or use isolated worktrees when the user has authorized that workflow.

## Accept only an approved implementation contract

In the fixed team workflow, read `.codex/spec.md` before planning. Require matching project identity, a `run_id`, `spec_version`, explicit acceptance criteria, and `status: APPROVED`. Do not infer missing product decisions from Leader conversation that was not handed off.

Pause and report to the Leader when implementation requires changing a public contract, schema, security model, important dependency, accepted non-goal, or material scope. Decide ordinary local implementation details yourself and record consequential decisions in `plan.md`.

Use the `leader_id`, `executor_worker_id`, Leader session, and Reviewer session recorded in `.codex/leader-state.md` when available. Update the Executor task binding at meaningful milestones and report concise status to the Leader; do not require the user to relay prompts.

## Create or resume the run

Resolve the project root with Git when possible. Store coordination artifacts under `<project-root>/.codex/`:

- `spec.md`: Leader-owned approved requirements and architecture; never edit it.
- `plan.md`: run identity, implementation state, task ownership, verification, and review request.
- `review.md`: the independent review verdict.

Allow only one active run per working tree. If `plan.md` describes another unfinished run, resume it when it is the same user task; otherwise stop and require a separate worktree or explicit resolution. Never silently overwrite an active run.

Use these states:

`PLANNING -> IMPLEMENTING -> INTEGRATING -> TESTING -> READY_FOR_REVIEW -> REVIEWING -> FIXING -> READY_FOR_REVIEW -> DONE`

Create a concise `plan.md` with this shape:

```markdown
---
run_id: <matching-spec-run-id>
status: IMPLEMENTING
review_iteration: 0
base_commit: <git-sha-or-none>
baseline_dirty_paths: [<pre-existing-paths-or-none>]
review_scope_paths: [<implementation-paths>]
review_fingerprint: pending
---

# Goal
<approved outcome from spec.md>

## Acceptance
- <observable acceptance criterion>

## Tasks
### task-id
- owner: <agent-name>
- depends_on: []
- allowed_paths: [<path/glob>]
- forbidden_paths: [<path/glob>]
- checks: [<command or evidence>]
- status: pending

## Integration checks
- <project-level command or evidence>

## Review notes
- <high-risk decisions and known constraints>
```

Preserve the acceptance criteria and invariants from `spec.md`. Update task statuses and implementation decisions, but do not turn `plan.md` into a transcript.

At run start, record the existing tracked, staged, and untracked non-ignored application changes. Preserve unrelated user changes. If an assigned file already has overlapping user edits that cannot be separated safely, pause that task and report the conflict to the Leader.

## Review the plan only when risk warrants it

The Leader owns architecture review before approval. If an approved spec still contains a blocking architectural ambiguity, pause and report it rather than silently redesigning.

## Dispatch minimum-sufficient task packets

Spawn subagents with no inherited conversation or only the smallest useful recent context. Give each subagent a self-contained packet containing:

```markdown
Goal:
Allowed paths:
Forbidden paths:
Required context and files:
Acceptance criteria:
Checks to run:
Return: status, changed files, checks, decisions, and remaining risks.
```

Explicitly tell each implementation subagent to:

- inspect before editing;
- stay inside its allowed paths;
- report before any necessary scope expansion;
- avoid editing `plan.md` and `review.md`;
- avoid Git commits;
- return a concise result rather than a work diary.

Do not assume subagent claims are correct. Inspect the resulting diff and rerun relevant checks in the Executor.

## Integrate and verify

After all implementation subagents finish:

1. Stop or account for every child agent.
2. Inspect `git status` and the complete diff, including untracked files.
3. Reject or resolve edits outside assigned ownership.
4. Reconcile shared interfaces and cross-layer contracts.
5. Run the project-level build, type checks, lint, and tests appropriate to the change.
6. Record commands and outcomes in `plan.md`.

Retry a failed or incomplete child once with corrected context. If it fails again, re-split the task or complete it sequentially. Escalate only genuine requirement ambiguity, missing authority, or unsafe scope expansion.

## Freeze and request independent review

Before review:

1. Ensure no implementation subagent is still writing.
2. Set `status: READY_FOR_REVIEW`.
3. Increment `review_iteration`.
4. Record the current `base_commit`.
5. Compute and record `review_fingerprint` with the bundled
   `../../scripts/review_fingerprint.py`, passing the project root and every
   review-scope path. This covers the run's tracked diff, staged diff, untracked
   non-ignored file list, and untracked file contents deterministically. If
   Python or the script is unavailable, stop and record
   `review_fingerprint: pending`; do not improvise a different algorithm.
6. Leave any earlier `review.md` untouched; identify the new request by run ID, iteration, and fingerprint so the Reviewer can replace it authoritatively.

Exclude `.codex/**` coordination artifacts from the implementation fingerprint and product diff. Also exclude only project-declared ignored/generated files. Record the exact review scope and exclusions in `plan.md`; never use exclusions to hide application changes.

Do not modify implementation files while status is `READY_FOR_REVIEW` or `REVIEWING`. The reviewer must verify the same `run_id`, iteration, and fingerprint before returning a formal verdict.

Do not spawn the reviewer as an ordinary implementation child: that weakens contextual independence and conflates execution with approval. Prefer a separately launched top-level Codex session with no inherited implementation conversation. If the environment provides an authorized session launcher, use it; otherwise use `plan.md` as the review request for the user's already-open reviewer session and wait for the matching `review.md`. Use an internal reviewer child only when the user explicitly accepts that fallback.

In the fixed team workflow, report `READY_FOR_REVIEW`, the run ID, iteration, and fingerprint to the Leader through the Executor binding. The Leader wakes the persistent Reviewer and routes its verdict. Do not ask the user to copy a review prompt. If operating standalone without a Leader, use the existing direct handoff fallback.

## Handle the verdict

- `PASS`: confirm the fingerprint still matches, set `status: DONE`, and report completion to the Leader.
- `REQUEST_CHANGES`: set `status: FIXING`, fix P0/P1 items and safe in-scope P2 items, rerun integration checks, then request another review.
- `STALE`: regenerate the fingerprint and request review again.
- Product ambiguity, material scope expansion, or a disputed architectural choice: pause and report to the Leader; ask the user directly only in standalone mode.

Allow at most two automatic fix-and-review cycles. On the third blocking verdict, report the evidence to the Leader for user resolution. P3 suggestions never block delivery unless they expose a concrete requirement violation.

When a CC-Panes worker binding is present, persist milestone progress there. After final `PASS`, set it to completed with a concise summary and call `report_to_leader`. Definition of done: all tasks are accounted for, no subagent is still running, ownership is respected, integration checks pass, and the Reviewer has issued `PASS` for the exact current fingerprint.

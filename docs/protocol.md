# Platform-neutral team protocol (0.2)

This document defines the reusable protocol. CLI- and orchestrator-specific behavior belongs in an adapter. The 0.2 default reference topology is three Codex sessions transported by CC-Panes: Leader, Executor, and Reviewer. A Claude Leader is an optional topology, never an installation side effect.

## Topology selection

Absent topology configuration resolves to `codex-three-pane`. Only an explicit
user-approved `claude-leader` selection permits the Claude adapter and journaled
authority migration to become active. Installing, upgrading, discovering, or
loading either native package does not select a topology and must not change live
panes, sessions, bindings, permissions, authority, schemas, or personal caches.

## Roles

### Leader

- Own user-facing discovery and architecture.
- Own the approved specification and durable recovery state.
- Dispatch implementation only after explicit approval.
- Route review results without asking the user to copy prompts.
- Never implement application code.

### Executor

- Consume one approved specification.
- Own implementation planning, delegation, integration, and verification.
- Keep child write scopes non-overlapping.
- Freeze a stable snapshot before requesting review.
- Pause on product, contract, security, schema, or material scope changes.

### Reviewer

- Remain independent from implementation reasoning.
- Inspect the exact frozen snapshot and evidence.
- Never implement fixes.
- Return PASS, REQUEST_CHANGES, or STALE.

## Required durable artifacts

An adapter may choose another storage format, but it must preserve equivalent data:

- specification identity, status, version, constraints, decisions, and acceptance criteria;
- current Leader generation and role/session bindings;
- implementation task ownership and verification evidence;
- review iteration, deterministic fingerprint, findings, and verdict;
- validated Leader succession checkpoint.

## Authority state

Specification:

~~~text
DISCOVERY → DESIGNING → PROPOSED → APPROVED → DONE
~~~

Implementation:

~~~text
PLANNING → IMPLEMENTING → INTEGRATING → TESTING
         → READY_FOR_REVIEW → REVIEWING
         → FIXING → READY_FOR_REVIEW → DONE
~~~

Review:

- PASS: the current fingerprint satisfies the approved specification.
- REQUEST_CHANGES: a stable snapshot contains actionable blocking defects.
- STALE: the snapshot changed or cannot be identified reliably.

## Adapter capability contract

An implementation adapter provides these semantic operations:

1. Resolve an exact project or worktree root.
2. Start or resume a top-level role session.
3. Deliver a prompt reliably after runtime bootstrap.
4. Observe session status and recent output.
5. Stop a superseded or failed session.
6. Persist parent/worker role bindings.
7. Deliver worker results to the current Leader.
8. Reconcile live role sessions, role descriptors, and bindings.
9. Spawn bounded internal implementation agents when supported.
10. Detect, or conservatively infer, context pressure.

The stable names are `resolveProject`, `launchRole`, `resumeRole`,
`deliverPrompt`, `observeSession`, `stopSession`, `bindRole`, `updateMilestone`,
`reportToLeader`, and `reconcile`. Each role descriptor names the role, CLI tool,
session/resume identity, exact project root, binding/parent IDs,
generation, and declared capabilities. A launcher selects the target descriptor's
CLI tool; it never assumes the caller and target use the same CLI.

Missing capabilities must degrade explicitly. An adapter must never pretend that unmanaged processes are durable bindings.

Normalized readiness evidence is exact terminal cwd, exact Git top-level, loaded
role Skill, received assignment or activation, and stable role/session/binding
identity. Authority is refused until reconciliation proves every item.

## Durable role and message state

In default `codex-three-pane`, the existing `leader-state.md` and TaskBindings are
authoritative and absence of `.codex/roles.json` is valid. For an explicitly
approved `claude-leader` migration, role-state schema 1 at `.codex/roles.json`
becomes authoritative for mutations and `leader-state.md` is its recovery mirror. It records workflow/run/migration identity,
Leader generation, migration phase, and exactly three descriptors. Schema 0 is the
absence of `roles.json` plus a valid legacy mirror and remains the valid default.
It may be upgraded only after explicit Claude-topology approval and exact live
validation. Partial, corrupt, or unknown state fails closed.

Every cross-role message uses envelope schema 1 with authority scope, message ID,
reply ID, run/migration/generation, message type, complete sender/recipient
identity, random nonce, timestamps, and inline payload or a digest-checked safe
payload path. Receivers acknowledge durable receipt separately from completion.
Expired, stale-generation, wrong-recipient, unknown-version, digest-mismatched, and
invalid-predecessor messages fail closed; same-key/same-hash retries replay a saved
terminal result, while same-key/different-hash is rejected.

Each role owns one logically append-only, hash-chained replay ledger. A deterministic
helper verifies writer identity and generation, serializes canonical UTF-8 JSON,
locks the ledger, flushes a temporary sibling, atomically replaces the canonical
file, and validates the whole prefix. The state machine is `RECEIVED -> EXECUTING ->
SUCCEEDED | FAILED | INDETERMINATE`. Before an external effect the receiver records
an operation key and classifies it as `PURE`, `RECONCILABLE_IDEMPOTENT`, or
`UNRECONCILABLE`. Recovery reads back a reconcilable effect before any retry;
ambiguity becomes `INDETERMINATE` and is never automatically repeated. This is
durable deduplication and fail-closed ambiguity, not a claim of exactly-once effects.

## Journaled authority migration

This section is dormant under the default topology. It applies only after explicit
user approval of `claude-leader`; package installation or Skill loading never
enters a migration phase.

One authoritative coordinator owns each migration-schema-1 journal. It records
ordered before/after snapshots around every mutation. The required phases are:

~~~text
CREATED -> PREFLIGHTED -> PREPARED -> VALIDATED
        -> NEW_LEADER_REGISTERED -> EXECUTOR_REPARENTED
        -> REVIEWER_REPARENTED -> STATE_ACTIVATED
        -> WORKERS_ACKED -> OLD_SUPERSEDED -> COMMITTED
~~~

After activation, both workers must read generation `N+1`, verify their parent,
and acknowledge the activation envelope. Thus `WORKERS_ACKED` precedes
`OLD_SUPERSEDED`, which in turn precedes `COMMITTED`. An exact-key final reconciliation then
proves the complete graph. Only after that read-back may a separately keyed
`bindRole` effect mark the old Leader `SUPERSEDED`. A separately keyed
`stopSession` effect completes `COMMITTED` and is the final CC-Panes effect. Each
of these three effects has its own durable `BEFORE`/`APPLIED` pair and recovery
read-back; missing or ambiguous evidence fails closed.

The pre-launch compatibility/capability gate must pass before a candidate exists.
The post-launch trust, identity, safe-write, delivery, and candidate-envelope gate
must also pass before protected mutation. Candidate validation uses an isolated
ledger and only `HANDSHAKE -> RECEIPT_ACK -> COMPLETION` under
`CANDIDATE_VALIDATION`; it cannot authorize normal role work or write the active
Leader ledger.

Before `STATE_ACTIVATED`, recovery restores the old generation and records
`ROLLED_BACK`. At or after activation, recovery rolls forward at a strictly higher
generation. A resumed coordinator compares live state with the last journal snapshot
and applies at most the one proven-absent mutation. The protocol claims cooperative
fencing among compliant sessions because CC-Panes currently has no compare-and-swap
generation guard.

## Workflow modes and review budget

`lean` is the default. It uses a compact approved spec, targeted verification, one
implementation review, and at most one delta-focused re-review of the original
blocking findings. Review cannot widen accepted scope. P2 is non-blocking unless
it demonstrates an approved acceptance violation, and implementation fixes or
review iterations do not change `spec_version`.

`assurance` is explicit opt-in for work that needs clean-install, complete
compatibility, migration, exhaustive fault-injection, or full E2E evidence. Those
gates are not default lean requirements unless the touched surface directly needs
them. Assurance availability does not authorize a Claude takeover or authority
migration.

## Lean behavior guard

Each lean run initializes one schema-1 JSON state at
`.codex/guards/<run-id>.json`. It records the run ID, `lean` mode, fixed limits,
and ordered idempotency events. A standard-library helper locks the run state,
validates the full event history, and atomically replaces the file for every
consumption or denial. Reusing the same action and operation key replays its saved
outcome without consuming twice; `same_failure_retry` also requires a failure key
and counts each failure independently.

The limits are `spec_revisions: 1`, `internal_agents: 0`,
`implementation_reviews: 1`, `focused_re_reviews: 1`,
`same_failure_retries: 2`, `full_test_suite_runs: 1`, and
`scope_expansions: 0`. Leader owns `spec_revision`, `scope_expansion`,
`implementation_review`, and `focused_re_review`; Executor owns `internal_agent`,
`same_failure_retry`, and `full_test_suite`; Reviewer verifies the matching review
event and never consumes another allowance or widens a focused re-review.

Denials persist evidence and fail closed with `SPEC_LIMIT_REACHED`,
`AGENT_LIMIT_REACHED`, `REVIEW_LIMIT_REACHED`, `RETRY_LIMIT_REACHED`,
`TEST_LIMIT_REACHED`, or `SCOPE_APPROVAL_REQUIRED`. A compliant role stops the
action and reports the code without mode escalation or another-CLI bypass. This is
deterministic protocol enforcement, not a security boundary for unmanaged writers.

## Invariants

- One user-facing Leader owns architecture at a time.
- One active implementation run exists per working tree.
- The Executor never edits Leader-owned specifications.
- The Reviewer never edits product implementation.
- A formal review always names a run, iteration, and fingerprint.
- PASS applies only to the exact reviewed fingerprint.
- Unknown product decisions are reopened with the user, never guessed.
- A replacement Leader is authoritative only after its takeover summary is validated.
- Cross-role prompts, receipts, milestones, reports, and verdicts are automatic; the user is never a message relay.
- Exactly three visible panes remain; migration may add only one temporary tab in the existing Leader pane.

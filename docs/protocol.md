# Platform-neutral team protocol

This document defines the reusable protocol. CLI- and orchestrator-specific behavior belongs in an adapter.

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

An implementation adapter should provide equivalent operations for:

1. Resolve an exact project or worktree root.
2. Start or resume a top-level role session.
3. Deliver a prompt reliably after runtime bootstrap.
4. Observe session status and recent output.
5. Stop a superseded or failed session.
6. Persist parent/worker role bindings.
7. Deliver worker results to the current Leader.
8. Spawn bounded internal implementation agents when supported.
9. Detect, or conservatively infer, context pressure.

Missing capabilities must degrade explicitly. An adapter must never pretend that unmanaged processes are durable bindings.

## Invariants

- One user-facing Leader owns architecture at a time.
- One active implementation run exists per working tree.
- The Executor never edits Leader-owned specifications.
- The Reviewer never edits product implementation.
- A formal review always names a run, iteration, and fingerprint.
- PASS applies only to the exact reviewed fingerprint.
- Unknown product decisions are reopened with the user, never guessed.
- A replacement Leader is authoritative only after its takeover summary is validated.

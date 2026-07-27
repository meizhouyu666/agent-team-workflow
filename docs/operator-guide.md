# Operator guide: install, upgrade, recovery, and evidence

Version 0.2.0 packages one authoritative `plugins/agent-team-workflow/skills/`
tree through independent native Claude Code and Codex descriptors. Do not copy or
fork the Skills between ecosystems. Skill frontmatter is the portable subset only:
`name` and `description`; provider UI metadata remains under `agents/` or native
manifests.

## Choose topology explicitly

With no topology setting, use `codex-three-pane`: Codex Leader, Codex Executor,
and Codex Reviewer. `claude-leader` is enabled only by explicit user approval.
Installing, upgrading, discovering, or loading either package is inert: it never
migrates schema, launches or stops a session, changes a pane or binding, changes
permissions or authority, or rewrites a personal plugin cache.

## Choose lean or assurance

Use `lean` unless the approved spec explicitly selects `assurance`. Lean runs
targeted checks, requests one implementation review, and permits at most one
focused re-review of the original blocking findings. Reviewer scope expansion and
P2 suggestions are backlog items unless they prove an approved acceptance
violation. Do not bump `spec_version` for fixes or review iterations.

Clean-install, complete compatibility, migration, exhaustive fault-injection, and
full E2E gates belong to explicit assurance mode or a directly touched surface.
Claude takeover remains deferred regardless of the validation budget.

## Validate and discover the native packages

From the tagged repository root, validate Claude's plugin directory and repository
marketplace independently:

~~~powershell
claude plugin validate --strict plugins/agent-team-workflow
claude plugin validate --strict .
claude --plugin-dir ./plugins/agent-team-workflow
claude --plugin-dir ./plugins/agent-team-workflow plugin details agent-team-workflow
~~~

In that fresh Claude session, invoke `/skills` and verify
`agent-team-workflow:lead-agent-workflow` is present. This proves package discovery
only; it does not select `claude-leader` or grant authority. `--plugin-dir` is the
development/smoke-test path and does not prove marketplace installation.

For native Claude marketplace discovery and installation:

~~~powershell
claude plugin marketplace add meizhouyu666/agent-team-workflow
claude plugin install agent-team-workflow@agent-team-workflow
claude plugin list
~~~

Restart Claude Code, inspect `/plugin`, then verify the namespaced Leader Skill in
`/skills`. A local checkout can be added as a marketplace by its absolute path and
`--scope local` for release-candidate validation.

For Codex, add the repository marketplace and plugin, then start a fresh top-level
session so the Skill cache is reloaded:

~~~powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
codex plugin list
~~~

Verify `$orchestrate-agent-team` and `$review-agent-work` are discovered. The Codex
and Claude manifests and marketplace entries must all say `0.2.0` and resolve the
same plugin directory. Each ecosystem validates only its own descriptor.

## Clean-install release check

Use disposable CLI configuration homes or machines. Add only the tagged repository,
install 0.2.0, clear/refresh the relevant plugin cache through the CLI's supported
install/update flow, and start new sessions. Do not reuse a role session that loaded
0.1 Skills. Record CLI/plugin versions, install source, discovered Skills, exact cwd
and Git root, and the matched compatibility row and file digest. A successful update
in an old process is not a clean-install result.

## Upgrade from 0.1 to 0.2

Updating the package does not update live roles. Keep the existing three-Codex
topology, legacy `leader-state.md`, sessions, panes, and bindings unchanged until
the operator deliberately starts fresh sessions. Loading 0.2.0 still does not
create `.codex/roles.json` or start migration.

Only after the user explicitly selects `claude-leader` may the current Codex Leader
freeze work, verify all three fresh 0.2 Skills and exact identities, validate the
supported compatibility row, upgrade valid schema-0 state, and run the two-gate
journaled Claude candidate migration. Invalid or partial state fails closed, and no
binding changes occur before both required gates permit them.

## Migration recovery

The generation `N` Codex Leader is the only migration coordinator. It writes an
atomic migration-schema-1 journal before/after every call and freezes role work.
Operators diagnose from that journal, `.codex/roles.json`, session output, panes,
TaskBindings, and reconciliation results; they do not repair state by hand while a
coordinator is live.

Before `STATE_ACTIVATED`, stop/quarantine a failed Claude candidate, close its
candidate ledger, restore the complete before-snapshot and old worker parents, and
record `ROLLED_BACK`. At or after activation, roll forward. If the Claude Leader is
lost, register a verified replacement as generation `N+2`; never lower the
generation or revive the old binding as `N`. Resume by comparing live state with
the last before/after snapshot and applying only a proven-absent step.

Post-launch trust, cwd, delivery, safe candidate-write, report, or handshake failure
must occur before graph mutation and must leave candidate evidence quarantined.
Unreconcilable external effects become `INDETERMINATE`; do not retry them blindly.

## Supported rollback and unsupported downgrade

A supported rollback to released 0.1 requires all of the following while 0.2 is
still running: commit/activate a verified Codex Leader, finish or abort every
migration, reconcile the three-role graph, export a validated schema-0-compatible
`leader-state.md` mirror into a clean schema-0 runtime namespace, reinstall 0.1,
refresh caches, and start three fresh 0.1 Codex sessions. Retain the finished 0.2
journal and schema-1 state as immutable recovery evidence; do not reinterpret it as
0.1 state.

Direct 0.2/schema-1 to 0.1 downgrade is **unsupported**. The 0.1 Skills cannot fence
schema-1 generations or refuse unknown message/migration state. Never point a 0.1
session at live schema-1 coordination state.

## Observability and deterministic E2E

The deterministic harness writes `.codex/e2e/<run-id>.json`; a model-authored
summary is not release evidence. Each record includes protocol/plugin/schema
versions, compatibility row and artifact digest, before/after role and binding
snapshots, pane/tab/session/CLI identities, tool request/response IDs and digests,
migration journal digest, envelope IDs/nonces/hashes, separate receipt/completion
acknowledgements, timestamps, probe results, assertions, command provenance, and
rollback/commit outcome. A `CROSS_CLI_HANDSHAKE` must change no product file and
must return the same run ID and nonce from both Codex bindings.

Fake transport tests exhaustively interrupt every journal write, ledger state, and
CC-Panes mutation. They inject wrong cwd, blocked trust, missing capability,
dropped/duplicate/expired/stale/wrong-recipient messages, reordered and digest-
mismatched retries, partial reparenting, concurrent stale writers, report during
cutover, resume loss, torn records, reconciled and unreconcilable effects, and failed
takeover. The oracle continuously asserts one cooperative authority generation,
exactly three panes, one writer/effect for keyed reconcilable operations, and a
deterministically recoverable parent graph.

The live proof is split at the authority boundary. During implementation review, a
`PRE_AUTHORITY` smoke may prove the exact supported tuple, native package discovery,
static tool schemas, project/layout resolution, launch history, the unchanged
three-pane/session/binding graph, safe read-back of the Executor's own milestone,
and report routing to the current Leader. Every observation is a correlated probe
record with an operation key, identities, canonical request/response hashes, and a
read-back outcome. A scalar `passed` value is never evidence.

`PRE_AUTHORITY` must report `FAIL` for a failed required probe and
`DEFERRED_POST_PASS`—never `PASS` or a silent skip—for operations intentionally not
performed before independent review: launching the Claude candidate tab, candidate
Skill/cwd/trust verification, the isolated candidate envelope/ledger lifecycle,
protected binding mutation, worker activation acknowledgement, and commit or
rollback. It must not create a pane, candidate binding, or authority mutation.

After the implementation fingerprint receives independent `PASS`, the authoritative
generation `N` Codex Leader may run the journaled live migration. That post-PASS
smoke proves both ordered gates, exactly one candidate tab in the existing Leader
pane, automatic cross-CLI prompt/TaskBinding/report routing, worker activation
acknowledgements, and a reconciled commit or rollback with observable IDs. Only the
combined pre-authority and post-PASS artifacts satisfy the operational live E2E;
neither widens the compatibility matrix or replaces deterministic fake fault
coverage.

## Troubleshooting checklist

- `No manifest found`: run validation against the plugin directory and repository
  root shown above; confirm both `.claude-plugin` paths exist.
- Skill missing after install: refresh/update the plugin and start a new top-level
  session; confirm the install source and version are 0.2.0.
- Candidate receives no prompt: inspect exact probe ID, nonce, and canonical hash in
  session output; absence fails the gate.
- Binding/report ambiguity: use collaboration read-back for the stable operation
  key. If completion or absence cannot be proved, record `INDETERMINATE`.
- Wrong cwd/Git root, pane/session drift, duplicate binding, or stale generation:
  quarantine the candidate or role and reconcile; never grant provisional authority.
- `.ccpanes/`: it is ignored runtime history/configuration, not product or E2E
  evidence. Do not edit, package, or delete it during release work.

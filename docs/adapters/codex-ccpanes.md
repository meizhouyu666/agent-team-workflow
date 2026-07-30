# CC-Panes Adapter (0.2)

This adapter runs exactly three role processes: Codex Leader, Codex Executor Mother,
and Codex Independent Reviewer. The default is `codex-three-pane`; despite its
legacy topology name, it does not prescribe a pane, tab, geometry, title, placement,
or layout. CC-Panes and the user own the UI.

A role descriptor is pane-independent: exact `project_root + role + session_id +
binding_id`, with CLI, generation, parent binding, capabilities, and verification
evidence. Reject duplicate role, session, or binding identities. Installation and
upgrade are inert: they do not select a topology or create `.codex/roles.json`.

## Required capabilities

A SUPPORTED adapter exposes project resolution, role launch/resume, session
status/output, TaskBinding updates, `report_to_leader`, reconciliation, and safe
session stop. TESTED_ONLY adapters may be used only with an explicit operator
decision. An UNSUPPORTED adapter must not be used for authority operations.

Use `launch_task` with the user's existing CC-Panes/Provider configuration; this
adapter makes no model selection or routing default. Validate cwd/Git root, Skill,
role identity, parent/generation, and capabilities before a replacement becomes
authoritative. A failure is INDETERMINATE until reconciliation proves otherwise.

## Blue-green Replacement

Checkpoint role state, launch one replacement, verify it, rebind and reconcile, then
stop the old role process. On any failed step, keep the old process alive. Rotate a
whole team in Executor, Reviewer, Leader order. When all roles are dead, a user must
start a fresh Leader from CC-Panes to recover durable state and launch the remaining
roles.

For packaged validation, use the repository plugin directory with
`--plugin-dir`. Schema activation remains `STATE_ACTIVATED` only for an explicitly
approved `claude-leader` authority transition; it is unrelated to UI placement.

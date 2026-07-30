from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-team-workflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import protocol_core as core  # noqa: E402
import protocol_harness as harness  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
NOW = "2026-07-26T12:00:00Z"
COMPATIBILITY_PATH = ROOT / "plugins" / "agent-team-workflow" / "compatibility.json"


def session(role: str, *, session_id: str | None = None) -> dict:
    return {
        "role": role,
        "cli_tool": "claude" if role == "leader" else "codex",
        "session_id": session_id or f"session-{role}",
        "project_root": r"E:\meizhouyu\agentstudy\agent-team-workflow",
    }


def binding(role: str, *, binding_id: str | None = None, parent: str | None = "binding-leader", authority: str = "FROZEN") -> dict:
    return {
        "role": role,
        "binding_id": binding_id or f"binding-{role}",
        "parent_binding_id": None if role == "leader" else parent,
        "leader_generation": 1,
        "authority_state": authority,
    }


def migration_journal(phase: str = "CREATED") -> dict:
    nullable = phase in ("CREATED", "PREFLIGHTED", "ABORTED")
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "migration_id": "migration-1",
        "phase": phase,
        "source_generation": 1,
        "target_generation": 2,
        "old_leader": {"binding_id": "binding-leader", "session_id": "session-leader"},
        "candidate": {
            "binding_id": None if nullable else "candidate-binding",
            "active_session_id": None if nullable else "candidate-session",
            "resume_lineage": None if nullable else "candidate-lineage",
        },
        "compatibility_row_id": "windows-local-claude-2.1.220-codex-0.144.6",
        "compatibility_digest": SHA_A,
        "before_state": {},
        "expected_binding_parents": {},
        "observed_binding_parents": {},
        "steps": [],
        "recovery_decision": None,
        "candidate_ledger_digest": None,
        "candidate_ledger_closed": False,
    }


def role_state() -> dict:
    roles = []
    for role in ("leader", "executor", "reviewer"):
        roles.append({
            "schema_version": 1,
            "role": role,
            "cli_tool": "codex",
            "adapter_id": "codex-ccpanes",
            "runtime_kind": "local",
            "project_root": r"E:\meizhouyu\agentstudy\agent-team-workflow",
            "session_id": f"session-{role}",
            "resume_id": None,
            "binding_id": f"binding-{role}",
            "parent_binding_id": None if role == "leader" else "binding-leader",
            "leader_generation": 1,
            "authority_state": "ACTIVE" if role == "leader" else "FROZEN",
            "required_capabilities": [],
            "advertised_capabilities": [],
            "verified_at": NOW,
        })
    return {
        "schema_version": 1,
        "workflow_version": "0.2.0",
        "run_id": "run-1",
        "leader_generation": 1,
        "migration_id": None,
        "migration_phase": None,
        "roles": roles,
    }


def assert_oracle(test: unittest.TestCase, transport: harness.FakeTransport) -> None:
    running = [item for item in transport.sessions.values() if item.get("running")]
    test.assertEqual({"leader", "executor", "reviewer"}, {item["role"] for item in running})
    test.assertEqual(len(running), len({item["session_id"] for item in running}))
    active = [item for item in transport.bindings.values() if item.get("authority_state") == "ACTIVE"]
    test.assertEqual(1, len(active))
    leader_binding = active[0]["binding_id"]
    for item in transport.bindings.values():
        if item["role"] != "leader" and item.get("authority_state") != "CANDIDATE":
            test.assertEqual(leader_binding, item["parent_binding_id"])


def configure_supported_gates(transport: harness.FakeTransport) -> None:
    artifact = core.validate_compatibility_artifact(
        json.loads(COMPATIBILITY_PATH.read_text(encoding="utf-8"))
    )
    context = {
        "run_id": "run-1", "migration_id": "migration-1",
        "source_generation": 1, "target_generation": 2,
        "coordinator_binding_id": "binding-leader",
        "coordinator_session_id": "session-leader",
        "candidate_binding_id": "candidate-binding",
        "candidate_session_id": "candidate-session",
        "evidence_started_at": "2026-07-26T12:00:00Z",
        "evidence_expires_at": "2026-07-26T13:00:00Z",
    }
    observed = harness._supported_gate_observed(
        artifact, "windows-local-claude-2.1.220-codex-0.144.6", context,
    )
    transport.configure_gate_evidence(artifact, observed)


def evidence() -> dict:
    return harness.run_supported_fake_e2e(
        ROOT, "run-1",
        COMPATIBILITY_PATH,
    )


class FakeTransportCrashTests(unittest.TestCase):
    def invoke(self, operation: str, transport: harness.FakeTransport) -> None:
        if operation == "launchRole":
            transport.launchRole("launch-key", session("leader"))
        elif operation == "resumeRole":
            transport.resumeRole("resume-key", session("leader", session_id="session-resumed"))
        elif operation == "deliverPrompt":
            transport.deliverPrompt("prompt-key", "session-leader", "probe-1", "nonce-1", SHA_A)
        elif operation == "stopSession":
            transport.stopSession("stop-key", "session-leader")
        elif operation == "bindRole":
            transport.bindRole("bind-key", binding("leader", authority="ACTIVE"))
        elif operation == "updateMilestone":
            transport.updateMilestone("milestone-key", "binding-leader", {"status": "READY"})
        elif operation == "reportToLeader":
            transport.reportToLeader("report-key", {"probe_id": "probe-1", "hash": SHA_A})
        else:  # pragma: no cover - table is closed below
            self.fail(operation)

    def seeded(self) -> harness.FakeTransport:
        transport = harness.FakeTransport()
        transport.sessions["session-leader"] = dict(session("leader"), running=True)
        transport.bindings["binding-leader"] = binding("leader", authority="ACTIVE")
        return transport

    def test_every_public_mutation_fault_hook_recovers_idempotently(self) -> None:
        operations = (
            "launchRole", "resumeRole", "deliverPrompt", "stopSession",
            "bindRole", "updateMilestone", "reportToLeader",
        )
        for operation in operations:
            for position in ("before", "after"):
                transport = self.seeded()
                transport.interrupt_at = f"{position}:{operation}"
                with self.subTest(operation=operation, position=position):
                    with self.assertRaises(harness.InjectedCrash):
                        self.invoke(operation, transport)
                    self.invoke(operation, transport)
                    matching = [call for call in transport.calls if call["operation"] == operation]
                    self.assertEqual(1, len(matching))

    def test_every_observation_fault_hook_is_executable(self) -> None:
        for operation in ("resolveProject", "observeSession", "reconcile"):
            for position in ("before", "after"):
                transport = self.seeded()
                transport.projects["project-root"] = {"project_root": "project-root"}
                transport.interrupt_at = f"{position}:{operation}"
                with self.subTest(operation=operation, position=position):
                    with self.assertRaises(harness.InjectedCrash):
                        if operation == "resolveProject":
                            transport.resolveProject("project-root")
                        elif operation == "observeSession":
                            transport.observeSession("session-leader")
                        else:
                            transport.reconcile()

    def test_every_journal_write_boundary_and_complete_commit_are_executable(self) -> None:
        for phase in core.MIGRATION_PHASES:
            for point in ("before", "after-before", "before-applied", "after"):
                transport = harness.FakeTransport(
                    interrupt_at=f"{point}:journal:{phase}"
                )
                with self.subTest(phase=phase, point=point):
                    with self.assertRaises(harness.InjectedCrash):
                        transport.checkpoint("journal", phase, point)

        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            journal.write(migration_journal())
            coordinator = harness.FakeMigrationCoordinator(harness.FakeTransport(), journal)

            for phase in core.COMMIT_PHASES:
                def prepare(value: dict, current_phase: str = phase) -> None:
                    if core.PHASE_ORDER[current_phase] >= core.PHASE_ORDER["PREPARED"]:
                        value["candidate"] = {
                            "binding_id": "candidate-binding",
                            "active_session_id": "candidate-session",
                            "resume_lineage": "candidate-lineage",
                        }
                    if current_phase == "COMMITTED":
                        value["candidate_ledger_digest"] = SHA_B
                        value["candidate_ledger_closed"] = True
                        value["recovery_decision"] = "COMMIT"
                        value["observed_binding_parents"] = copy.deepcopy(
                            value["expected_binding_parents"]
                        )

                coordinator.write_phase(phase, prepare)

            committed = journal.load()
            self.assertEqual("COMMITTED", committed["phase"])
            self.assertEqual(len(core.COMMIT_PHASES) * 2, len(committed["steps"]))
            terminal_mutation = copy.deepcopy(committed)
            terminal_mutation["phase"] = "ABORTED"
            with self.assertRaisesRegex(core.ProtocolError, "TERMINAL_JOURNAL_IMMUTABLE"):
                journal.write(terminal_mutation)

        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            journal.write(migration_journal())
            interrupted = harness.FakeMigrationCoordinator(
                harness.FakeTransport(interrupt_at="after-before:journal:PREFLIGHTED"),
                journal,
            )
            with self.assertRaises(harness.InjectedCrash):
                interrupted.write_phase("PREFLIGHTED")
            self.assertEqual("CREATED", journal.load()["phase"])
            harness.FakeMigrationCoordinator(harness.FakeTransport(), journal).write_phase(
                "PREFLIGHTED"
            )
            self.assertEqual("PREFLIGHTED", journal.load()["phase"])

    def test_every_protected_mutation_hook_and_gate_is_executable(self) -> None:
        for name in harness.FakeMigrationCoordinator.PROTECTED_MUTATIONS:
            blocked_transport = harness.FakeTransport()
            blocked = harness.FakeMigrationCoordinator(
                blocked_transport,
                core.MigrationJournal(Path(tempfile.gettempdir()) / "unused-migration.json"),
            )
            with self.subTest(name=name, gate="blocked"):
                with self.assertRaisesRegex(core.ProtocolError, "PROTECTED_OPERATION_FORBIDDEN"):
                    blocked.protected_mutation(name, lambda: None)

            for position in ("before", "after"):
                with tempfile.TemporaryDirectory() as raw:
                    journal = core.MigrationJournal(Path(raw) / "migration.json")
                    journal.write(migration_journal())
                    transport = harness.FakeTransport(
                        interrupt_at=f"{position}:mutation:{name}"
                    )
                    configure_supported_gates(transport)
                    coordinator = harness.FakeMigrationCoordinator(transport, journal)
                    applied: list[str] = []
                    with self.subTest(name=name, position=position):
                        with self.assertRaises(harness.InjectedCrash):
                            coordinator.protected_mutation(name, lambda: applied.append(name))
                        self.assertEqual([] if position == "before" else [name], applied)

        fabricated = harness.FakeTransport()
        fabricated.pre_launch_passed = True
        fabricated.post_launch_passed = True
        with self.assertRaisesRegex(core.ProtocolError, "PROTECTED_OPERATION_FORBIDDEN"):
            fabricated.require_protected_gate()

    def test_idempotency_key_conflict_never_applies_second_effect(self) -> None:
        transport = self.seeded()
        transport.deliverPrompt("same-key", "session-leader", "probe-1", "nonce-1", SHA_A)
        with self.assertRaisesRegex(core.ProtocolError, "IDEMPOTENCY_CONFLICT"):
            transport.deliverPrompt("same-key", "session-leader", "probe-2", "nonce-2", SHA_B)
        self.assertEqual(1, len(transport.calls))

    def test_report_during_cutover_is_refused_before_transport_effect(self) -> None:
        state = role_state()
        state["migration_id"] = "migration-1"
        state["migration_phase"] = "EXECUTOR_REPARENTED"
        transport = self.seeded()
        with self.assertRaisesRegex(core.ProtocolError, "ROLE_WORK_FROZEN"):
            core.authorize_role_work(state, "executor", 1)
            transport.reportToLeader(
                "cutover-report",
                {"binding_id": "binding-executor", "status": "must-not-send"},
            )
        self.assertEqual({}, transport.reports)
        self.assertEqual([], transport.calls)

    def test_receipt_effect_and_terminal_crash_windows_reconcile_to_one_effect(self) -> None:
        desired = {
            "session_id": "session-executor",
            "probe_id": "probe-1",
            "nonce": "nonce-1",
            "request_hash": SHA_A,
        }
        for position in ("before", "after"):
            with tempfile.TemporaryDirectory() as raw:
                ledger = core.Ledger(
                    Path(raw) / "executor.jsonl", role_state,
                    "executor", "binding-executor", "session-executor", 1,
                )
                request_key = ("binding-leader", "run-1", 1, "message-1")
                ledger.append(request_key, SHA_A, "RECEIVED", recorded_at=NOW)
                ledger.append(
                    request_key, SHA_A, "EXECUTING",
                    operation_key="effect-key",
                    effect_policy=core.EffectPolicy.RECONCILABLE_IDEMPOTENT,
                    readback_predicate="prompt_output_has_probe_id_nonce_hash",
                    recorded_at="2026-07-26T12:00:01Z",
                )
                transport = harness.FakeTransport(
                    interrupt_at=f"{position}:deliverPrompt"
                )
                with self.assertRaises(harness.InjectedCrash):
                    transport.deliverPrompt(
                        "effect-key", "session-executor", "probe-1", "nonce-1", SHA_A,
                    )

                observation = core.Observation(
                    transport.observe_effect("effect-key", desired)
                )
                terminal, outcome = core.reconcile_unresolved(
                    core.EffectPolicy.RECONCILABLE_IDEMPOTENT,
                    observation,
                    adapter_honors_key=True,
                    execute=lambda: transport.deliverPrompt(
                        "effect-key", "session-executor", "probe-1", "nonce-1", SHA_A,
                    ),
                )
                # Simulate process loss before terminal persistence: a fresh
                # ledger instance reconciles the unresolved EXECUTING record.
                restarted = core.Ledger(
                    ledger.path, role_state,
                    "executor", "binding-executor", "session-executor", 1,
                )
                restarted.append(
                    request_key, SHA_A, terminal, outcome=outcome,
                    recorded_at="2026-07-26T12:00:02Z",
                )
                # Replaying after a crash during terminal persistence returns the
                # saved outcome and cannot execute the external effect again.
                replay = restarted.append(
                    request_key, SHA_A, "RECEIVED",
                    recorded_at="2026-07-26T12:00:03Z",
                )
                with self.subTest(position=position):
                    self.assertEqual("SUCCEEDED", replay["processing_state"])
                    self.assertEqual(1, len(transport.calls))
                    self.assertEqual(3, len(restarted.records()))

    def test_resume_failure_and_failed_takeover_roll_back_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration_journal("PREPARED"))
            transport = harness.FakeTransport()
            for role in ("leader", "executor", "reviewer"):
                transport.sessions[f"session-{role}"] = dict(session(role), running=True)
            transport.sessions["candidate-session"] = dict(
                session(
                    "leader", session_id="candidate-session",
                ),
                running=True,
            )
            transport.bindings = {
                "binding-leader": binding("leader", authority="ACTIVE"),
                "binding-executor": binding("executor"),
                "binding-reviewer": binding("reviewer"),
                "candidate-binding": binding(
                    "leader", binding_id="candidate-binding", authority="CANDIDATE",
                ),
            }
            transport.configure_oracle(
                role_sessions=[("leader", "session-leader"), ("leader", "candidate-session"), ("executor", "session-executor"), ("reviewer", "session-reviewer")],
                authority_generation=1,
                leader_binding_ids=["binding-leader", "candidate-binding"],
                worker_binding_ids=["binding-executor", "binding-reviewer"],
                recoverable_parent_ids=["binding-leader", "candidate-binding"],
            )

            # Simulate a partial reparent followed by a failed candidate resume.
            transport.bindRole(
                "move-executor",
                binding("executor", parent="candidate-binding"),
            )
            transport.interrupt_at = "before:resumeRole"
            replacement = session(
                "leader", session_id="replacement-session",
            )
            with self.assertRaises(harness.InjectedCrash):
                transport.resumeRole("resume-candidate", replacement)
            self.assertNotIn("replacement-session", transport.sessions)
            self.assertEqual("ROLL_BACK", journal.recovery_direction())

            # Pre-activation recovery restores generation N, quarantines candidate
            # evidence, and stops the failed candidate without losing a pane.
            transport.bindRole(
                "restore-executor",
                binding("executor", parent="binding-leader"),
            )
            transport.stopSession("stop-candidate", "candidate-session")
            candidate_ledger = root / "candidate.jsonl"
            final_digest = journal.close_candidate_ledger(
                candidate_ledger, recovery_decision="ROLL_BACK",
            )
            rolled_back = journal.load()
            rolled_back["phase"] = "ROLLED_BACK"
            journal.write(rolled_back)

            transport.reconcile()
            self.assertEqual("ROLLED_BACK", journal.load()["phase"])
            self.assertTrue(journal.load()["candidate_ledger_closed"])
            self.assertEqual(hashlib.sha256(b"").hexdigest(), final_digest)
            self.assertFalse(transport.sessions["candidate-session"]["running"])
            self.assertEqual(
                "binding-leader",
                transport.bindings["binding-executor"]["parent_binding_id"],
            )
            self.assertEqual(
                "binding-leader",
                transport.bindings["binding-reviewer"]["parent_binding_id"],
            )

    def test_partial_reparent_crash_has_deterministic_parent_recovery(self) -> None:
        transport = harness.FakeTransport()
        for role in ("leader", "executor", "reviewer"):
            transport.sessions[f"session-{role}"] = dict(session(role), running=True)
        transport.bindings = {
            "binding-leader": binding("leader", authority="ACTIVE"),
            "binding-executor": binding("executor"),
            "binding-reviewer": binding("reviewer"),
        }
        transport.configure_oracle(
            role_sessions=[("leader", "session-leader"), ("leader", "candidate-session"), ("executor", "session-executor"), ("reviewer", "session-reviewer")],
            authority_generation=1,
            leader_binding_ids=["binding-leader", "candidate-binding"],
            worker_binding_ids=["binding-executor", "binding-reviewer"],
            recoverable_parent_ids=["binding-leader", "candidate-binding"],
        )
        assert_oracle(self, transport)

        # A candidate is tracked by its role/session/binding identity only.
        candidate_session = session(
            "leader", session_id="candidate-session",
        )
        candidate_binding = binding(
            "leader", binding_id="candidate-binding", authority="CANDIDATE",
        )
        transport.launchRole("launch-candidate", candidate_session)
        transport.bindRole("bind-candidate", candidate_binding)
        assert_oracle(self, transport)

        moved = binding("executor", parent="candidate-binding")
        transport.bindRole("move-executor", moved)
        # Recovery before STATE_ACTIVATED deterministically restores generation N.
        restored = binding("executor", parent="binding-leader")
        transport.bindRole("rollback-executor", restored)
        assert_oracle(self, transport)
        self.assertEqual("binding-leader", transport.bindings["binding-executor"]["parent_binding_id"])
        self.assertEqual("binding-leader", transport.bindings["binding-reviewer"]["parent_binding_id"])

    def test_oracle_fails_closed_for_role_process_and_parent_violations(self) -> None:
        def configured() -> harness.FakeTransport:
            transport = harness.FakeTransport()
            for role in ("leader", "executor", "reviewer"):
                transport.sessions[f"session-{role}"] = dict(session(role), running=True)
            transport.bindings = {
                "binding-leader": binding("leader", authority="ACTIVE"),
                "binding-executor": binding("executor"),
                "binding-reviewer": binding("reviewer"),
            }
            transport.configure_oracle(
                role_sessions=[("leader", "session-leader"), ("leader", "candidate-session"), ("executor", "session-executor"), ("reviewer", "session-reviewer")],
                authority_generation=1,
                leader_binding_ids=["binding-leader", "candidate-binding"],
                worker_binding_ids=["binding-executor", "binding-reviewer"],
                recoverable_parent_ids=["binding-leader", "candidate-binding"],
            )
            return transport

        extra_role_process = configured()
        extra_role_process.sessions["unexpected"] = dict(
            session("leader", session_id="unexpected"), running=True,
        )
        with self.assertRaisesRegex(core.ProtocolError, "ROLE_PROCESS_INVARIANT"):
            extra_role_process.reconcile()

        duplicate_authority = configured()
        duplicate_authority.bindings["candidate-binding"] = {
            **binding("leader", binding_id="candidate-binding", authority="ACTIVE"),
            "leader_generation": 1,
        }
        with self.assertRaisesRegex(core.ProtocolError, "AUTHORITY_INVARIANT"):
            duplicate_authority.reconcile()

        bad_parent = configured()
        bad_parent.bindings["binding-executor"]["parent_binding_id"] = "unknown-leader"
        with self.assertRaisesRegex(core.ProtocolError, "PARENT_GRAPH_INVARIANT"):
            bad_parent.reconcile()

    def test_transition_oracle_rejects_early_active_missing_readback_supersede_stop_and_reconcile(self) -> None:
        before = harness._fake_role_state(ROOT, "run-1", "migration-1", 1, "codex", "CREATED")
        activated = harness._fake_role_state(ROOT, "run-1", "migration-1", 2, "claude", "STATE_ACTIVATED")
        old, new = before["roles"][0], activated["roles"][0]

        def configured() -> harness.FakeTransport:
            transport = harness.FakeTransport()
            for role in before["roles"]:
                transport.sessions[role["session_id"]] = {
                    key: role[key] for key in ("role", "cli_tool", "session_id")
                } | {"running": True}
                transport.bindings[role["binding_id"]] = {
                    "role": role["role"], "binding_id": role["binding_id"],
                    "parent_binding_id": role["parent_binding_id"],
                    "leader_generation": 1, "authority_state": role["authority_state"],
                }
            transport.sessions[new["session_id"]] = {
                key: new[key] for key in ("role", "cli_tool", "session_id")
            } | {"running": True}
            transport.bindings[new["binding_id"]] = {
                "role": "leader", "binding_id": new["binding_id"],
                "parent_binding_id": None, "leader_generation": 2,
                "authority_state": "CANDIDATE",
            }
            transport.configure_migration_oracle(
                role_sessions=[(role["role"], role["session_id"]) for role in before["roles"]] + [(new["role"], new["session_id"])],
                source_generation=1, target_generation=2,
                old_leader_binding_id=old["binding_id"],
                new_leader_binding_id=new["binding_id"],
                old_leader_session_id=old["session_id"],
                worker_binding_ids=[role["binding_id"] for role in before["roles"][1:]],
                candidate_binding_ids=[new["binding_id"]],
                recoverable_parent_ids=[old["binding_id"], new["binding_id"]],
                role_state=before,
            )
            return transport

        early_active = configured()
        with self.assertRaisesRegex(core.ProtocolError, "AUTHORITY_INVARIANT|DUAL_ACTIVE|EARLY_ACTIVE"):
            early_active.bindRole("early-active", {
                "role": "leader", "binding_id": new["binding_id"],
                "parent_binding_id": None, "leader_generation": 2,
                "authority_state": "ACTIVE",
            })

        missing_readback = configured()
        missing_readback.role_state_readback = None
        with self.assertRaisesRegex(core.ProtocolError, "MISSING_ROLE_STATE_READBACK"):
            missing_readback.reconcile()

        early_stop = configured()
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_OLD_STOP"):
            early_stop.stopSession("early-stop", old["session_id"])

        def activated_transport() -> harness.FakeTransport:
            transport = configured()
            for worker in activated["roles"][1:]:
                transport.bindRole(f"move-{worker['role']}", {
                    "role": worker["role"], "binding_id": worker["binding_id"],
                    "parent_binding_id": new["binding_id"], "leader_generation": 2,
                    "authority_state": "FROZEN",
                })
            transport.activateRoleState("activate", activated)
            return transport

        early_supersede = activated_transport()
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_OLD_SUPERSEDE"):
            early_supersede.bindRole("early-supersede", {
                "role": "leader", "binding_id": old["binding_id"],
                "parent_binding_id": None, "leader_generation": 1,
                "authority_state": "SUPERSEDED",
            })

        early_reconcile = activated_transport()
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_FINAL_RECONCILE"):
            early_reconcile.reconcile(key="early-final-reconcile", final=True)

    def test_final_effects_reconcile_before_and_after_crash_exactly_once(self) -> None:
        def seed_journal(path: Path, target_phase: str, *, terminal_ready: bool) -> core.MigrationJournal:
            journal = core.MigrationJournal(path)
            journal.write(migration_journal())
            seed = harness.FakeMigrationCoordinator(harness.FakeTransport(), journal)
            for phase in core.COMMIT_PHASES:
                if core.PHASE_ORDER[phase] > core.PHASE_ORDER[target_phase]:
                    break

                def prepare(value: dict, current_phase: str = phase) -> None:
                    if core.PHASE_ORDER[current_phase] >= core.PHASE_ORDER["PREPARED"]:
                        value["candidate"] = {
                            "binding_id": "candidate-binding",
                            "active_session_id": "candidate-session",
                            "resume_lineage": "candidate-lineage",
                        }

                seed.write_phase(phase, prepare)
            if terminal_ready:
                value = journal.load()
                value["candidate_ledger_digest"] = SHA_B
                value["candidate_ledger_closed"] = True
                value["recovery_decision"] = "COMMIT"
                value["observed_binding_parents"] = copy.deepcopy(value["expected_binding_parents"])
                journal.write(value)
            return journal

        def seeded_transport() -> harness.FakeTransport:
            transport = harness.FakeTransport()
            transport.sessions["session-leader"] = dict(session("leader"), running=True)
            transport.bindings["binding-leader"] = binding(
                "leader", binding_id="binding-leader", authority="FROZEN",
            )
            transport.bindings["candidate-binding"] = {
                "role": "leader", "binding_id": "candidate-binding",
                "parent_binding_id": None, "leader_generation": 2,
                "authority_state": "ACTIVE",
            }
            configure_supported_gates(transport)
            return transport

        for effect_name, operation, phase, target_phase, terminal_ready, advance_phase in (
            ("final_reconcile", "reconcile", "OLD_SUPERSEDED", "WORKERS_ACKED", False, False),
            ("supersede_old", "bindRole", "OLD_SUPERSEDED", "WORKERS_ACKED", False, True),
            ("stop_old", "stopSession", "COMMITTED", "OLD_SUPERSEDED", True, True),
        ):
            for crash_side in ("before", "after"):
                with self.subTest(effect=effect_name, crash_side=crash_side), tempfile.TemporaryDirectory() as raw:
                    journal = seed_journal(
                        Path(raw) / "migration.json", target_phase,
                        terminal_ready=terminal_ready,
                    )
                    transport = seeded_transport()
                    coordinator = harness.FakeMigrationCoordinator(transport, journal)
                    key = core.operation_key("migration-1", effect_name, operation)
                    if effect_name == "final_reconcile":
                        desired = {"final": True}
                        predicate = "complete_generation_graph_with_old_fenced"
                        effect = lambda: coordinator.protected_mutation(
                            "final_reconcile",
                            lambda: transport.reconcile(key=key, final=True),
                            reconcile_around=False,
                        )
                        readback = transport.reconciliation_readback
                        valid = lambda value: value.get("final_reconciled") is True
                    elif effect_name == "supersede_old":
                        desired = {
                            "role": "leader", "binding_id": "binding-leader",
                            "parent_binding_id": None, "leader_generation": 1,
                            "authority_state": "SUPERSEDED",
                        }
                        predicate = "old_binding_exactly_superseded"
                        effect = lambda: coordinator.protected_mutation(
                            "supersede_old", lambda: transport.bindRole(key, desired),
                            reconcile_around=False,
                        )
                        readback = lambda: copy.deepcopy(transport.bindings["binding-leader"])
                        valid = lambda value: dict(value) == desired
                    else:
                        desired = {"session_id": "session-leader", "running": False}
                        predicate = "old_session_exactly_stopped"
                        effect = lambda: coordinator.protected_mutation(
                            "stop_old", lambda: transport.stopSession(key, "session-leader"),
                            reconcile_around=False,
                        )
                        readback = lambda: {
                            "session_id": "session-leader",
                            "running": bool(transport.sessions["session-leader"]["running"]),
                        }
                        valid = lambda value: dict(value) == desired

                    kwargs = {
                        "phase": phase, "key": key, "desired": desired,
                        "readback_predicate": predicate, "effect": effect,
                        "readback": readback, "validate_readback": valid,
                        "timestamp": lambda: NOW, "advance_phase": advance_phase,
                    }
                    transport.interrupt_at = f"{crash_side}:{operation}"
                    with self.assertRaises(harness.InjectedCrash):
                        coordinator.journaled_effect(**kwargs)
                    self.assertEqual(
                        ["BEFORE"],
                        [step["status"] for step in journal.load()["steps"] if step["operation"] == key],
                    )
                    recovered = coordinator.journaled_effect(**kwargs)
                    self.assertTrue(valid(recovered))
                    pair = [step for step in journal.load()["steps"] if step["operation"] == key]
                    self.assertEqual(["BEFORE", "APPLIED"], [step["status"] for step in pair])
                    self.assertEqual(recovered, pair[1]["after"]["readback"])
                    self.assertEqual(1, len([call for call in transport.calls if call["operation"] == operation]))

    def test_final_effect_recovery_fails_closed_on_unknown_or_missing_readback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            value = migration_journal("WORKERS_ACKED")
            value["candidate"] = {
                "binding_id": "candidate-binding", "active_session_id": "candidate-session",
                "resume_lineage": "candidate-lineage",
            }
            journal.write(value)
            transport = self.seeded()
            configure_supported_gates(transport)
            coordinator = harness.FakeMigrationCoordinator(transport, journal)
            key = core.operation_key("migration-1", "final_reconcile", "reconcile")
            desired = {"final": True}
            transport.effects[key] = {"operation": "reconcile", "desired_digest": core.digest({"final": False})}
            with self.assertRaisesRegex(core.ProtocolError, "INDETERMINATE_FINAL_EFFECT"):
                coordinator.journaled_effect(
                    phase="OLD_SUPERSEDED", key=key, desired=desired,
                    readback_predicate="complete_generation_graph_with_old_fenced",
                    effect=lambda: transport.reconcile(key=key, final=True),
                    readback=lambda: {}, validate_readback=lambda value: bool(value),
                    timestamp=lambda: NOW, advance_phase=False,
                )
            self.assertEqual([], transport.calls)

            transport.effects[key] = {"operation": "reconcile", "desired_digest": core.digest(desired)}
            with self.assertRaisesRegex(core.ProtocolError, "INDETERMINATE_FINAL_EFFECT"):
                coordinator.journaled_effect(
                    phase="OLD_SUPERSEDED", key=key, desired=desired,
                    readback_predicate="complete_generation_graph_with_old_fenced",
                    effect=lambda: transport.reconcile(key=key, final=True),
                    readback=lambda: {}, validate_readback=lambda value: bool(value),
                    timestamp=lambda: NOW, advance_phase=False,
                )
            self.assertEqual(
                ["BEFORE"],
                [step["status"] for step in journal.load()["steps"] if step["operation"] == key],
            )

    def test_final_effect_applied_recovery_rejects_every_tampered_correlation_field(self) -> None:
        def effect_case(effect_name: str) -> tuple[str, str, str, dict, str, dict, object]:
            if effect_name == "final_reconcile":
                desired = {"final": True}
                readback = {"final_reconciled": True}
                return (
                    "WORKERS_ACKED", "OLD_SUPERSEDED", "reconcile", desired,
                    "complete_generation_graph_with_old_fenced", readback,
                    lambda value: value.get("final_reconciled") is True,
                )
            if effect_name == "supersede_old":
                desired = {
                    "role": "leader", "binding_id": "binding-leader",
                    "parent_binding_id": None, "leader_generation": 1,
                    "authority_state": "SUPERSEDED",
                }
                return (
                    "OLD_SUPERSEDED", "OLD_SUPERSEDED", "bindRole", desired,
                    "old_binding_exactly_superseded", desired,
                    lambda value: dict(value) == desired,
                )
            desired = {"session_id": "session-leader", "running": False}
            return (
                "COMMITTED", "COMMITTED", "stopSession", desired,
                "old_session_exactly_stopped", desired,
                lambda value: dict(value) == desired,
            )

        def valid_journal(
            journal_phase: str, phase: str, key: str, desired: dict,
            predicate: str, readback: dict,
        ) -> dict:
            terminal = phase == "COMMITTED"
            value = migration_journal(journal_phase)
            if terminal:
                value["candidate_ledger_digest"] = SHA_B
                value["candidate_ledger_closed"] = True
                value["recovery_decision"] = "COMMIT"
                value["observed_binding_parents"] = copy.deepcopy(value["expected_binding_parents"])
                for seeded_phase in core.COMMIT_PHASES:
                    operation = f"seed-{seeded_phase.lower()}"
                    value["steps"].extend([
                        {
                            "sequence": len(value["steps"]) + 1,
                            "phase": seeded_phase, "operation": operation,
                            "status": "BEFORE", "before": {}, "after": None,
                            "recorded_at": NOW,
                        },
                        {
                            "sequence": len(value["steps"]) + 2,
                            "phase": seeded_phase, "operation": operation,
                            "status": "APPLIED", "before": None, "after": {},
                            "recorded_at": NOW,
                        },
                    ])
            desired_sha256 = core.digest(desired)
            value["steps"].extend([
                {
                    "sequence": len(value["steps"]) + 1,
                    "phase": phase, "operation": key, "status": "BEFORE",
                    "before": {
                        "operation_key": key, "desired_sha256": desired_sha256,
                        "readback_predicate": predicate,
                    },
                    "after": None, "recorded_at": NOW,
                },
                {
                    "sequence": len(value["steps"]) + 2,
                    "phase": phase, "operation": key, "status": "APPLIED",
                    "before": None,
                    "after": {
                        "operation_key": key, "desired_sha256": desired_sha256,
                        "readback_predicate": predicate,
                        "readback": copy.deepcopy(readback),
                        "readback_sha256": core.digest(readback),
                        "outcome": "SUCCEEDED",
                    },
                    "recorded_at": NOW,
                },
            ])
            return core.validate_migration_journal(value)

        variants = (
            *(f"after_missing:{field}" for field in (
                "operation_key", "desired_sha256", "readback_predicate",
                "readback", "readback_sha256", "outcome",
            )),
            *(f"before_missing:{field}" for field in (
                "operation_key", "desired_sha256", "readback_predicate",
            )),
            *(f"after_wrong:{field}" for field in (
                "operation_key", "desired_sha256", "readback_predicate",
                "readback", "readback_sha256", "outcome",
            )),
            "both_wrong:desired_sha256", "both_wrong:readback_predicate",
            "semantic_readback", "duplicate_applied", "reordered",
        )
        core_accepts_structural_tuple = {
            "both_wrong:desired_sha256", "both_wrong:readback_predicate",
            "semantic_readback",
        }

        for effect_name in ("final_reconcile", "supersede_old", "stop_old"):
            journal_phase, phase, operation, desired, predicate, readback, valid = effect_case(effect_name)
            key = core.operation_key("migration-1", effect_name, operation)
            for variant in variants:
                with self.subTest(effect=effect_name, tamper=variant), tempfile.TemporaryDirectory() as raw:
                    value = valid_journal(
                        journal_phase, phase, key, desired, predicate, readback,
                    )
                    pair = [step for step in value["steps"] if step["operation"] == key]
                    before, after = pair[0]["before"], pair[1]["after"]
                    mode, _, field = variant.partition(":")
                    if mode == "after_missing":
                        after.pop(field)
                    elif mode == "before_missing":
                        before.pop(field)
                    elif mode == "after_wrong":
                        after[field] = {
                            "operation_key": "wrong-key",
                            "desired_sha256": SHA_A,
                            "readback_predicate": "wrong_predicate",
                            "readback": None,
                            "readback_sha256": SHA_A,
                            "outcome": "FAILED",
                        }[field]
                    elif mode == "both_wrong":
                        replacement = SHA_A if field == "desired_sha256" else "wrong_predicate"
                        before[field] = replacement
                        after[field] = replacement
                    elif mode == "semantic_readback":
                        after["readback"] = {"unexpected": True}
                        after["readback_sha256"] = core.digest(after["readback"])
                    elif mode == "duplicate_applied":
                        duplicate = copy.deepcopy(pair[1])
                        duplicate["sequence"] = len(value["steps"]) + 1
                        value["steps"].append(duplicate)
                    elif mode == "reordered":
                        first = value["steps"].index(pair[0])
                        second = value["steps"].index(pair[1])
                        value["steps"][first], value["steps"][second] = (
                            value["steps"][second], value["steps"][first],
                        )
                        for sequence, step in enumerate(value["steps"], 1):
                            step["sequence"] = sequence

                    if variant in core_accepts_structural_tuple:
                        core.validate_migration_journal(value)
                    else:
                        with self.assertRaises(core.ProtocolError):
                            core.validate_migration_journal(value)

                    path = Path(raw) / "migration.json"
                    path.write_bytes(core.canonical_bytes(value) + b"\n")
                    journal = core.MigrationJournal(path)
                    transport = self.seeded()
                    coordinator = harness.FakeMigrationCoordinator(transport, journal)
                    if operation == "reconcile":
                        effect = lambda: transport.reconcile(key=key, final=True)
                    elif operation == "bindRole":
                        effect = lambda: transport.bindRole(key, desired)
                    else:
                        effect = lambda: transport.stopSession(key, "session-leader")
                    with self.assertRaises(core.ProtocolError):
                        coordinator.journaled_effect(
                            phase=phase, key=key, desired=desired,
                            readback_predicate=predicate, effect=effect,
                            readback=lambda: copy.deepcopy(readback),
                            validate_readback=valid, timestamp=lambda: NOW,
                            advance_phase=effect_name != "final_reconcile",
                        )
                    self.assertEqual([], transport.calls)


class E2EEvidenceTests(unittest.TestCase):
    def test_deterministic_write_and_validation_round_trip(self) -> None:
        value = evidence()
        validated = harness.validate_e2e_evidence(value)
        self.assertEqual(value, validated)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "run-1.json"
            first = harness.write_e2e_evidence(path, value)
            bytes_first = path.read_bytes()
            second = harness.write_e2e_evidence(path, copy.deepcopy(value))
            self.assertEqual(first, second)
            self.assertEqual(bytes_first, path.read_bytes())
            self.assertEqual(value, json.loads(path.read_text(encoding="utf-8")))

    def test_missing_command_provenance_fails_closed(self) -> None:
        invalid = evidence()
        invalid["command_results"] = []
        with self.assertRaises(core.ProtocolError):
            harness.validate_e2e_evidence(invalid)

    def test_tampered_digest_and_tool_provenance_fail_closed(self) -> None:
        invalid_digest = evidence()
        invalid_digest["assertions"][0]["evidence_digest"] = "not-a-digest"
        with self.assertRaises(core.ProtocolError):
            harness.validate_e2e_evidence(invalid_digest)

        missing_request = evidence()
        del missing_request["tool_events"][0]["request_id"]
        with self.assertRaises(core.ProtocolError):
            harness.validate_e2e_evidence(missing_request)

        tampered_request = evidence()
        tampered_request["tool_events"][0]["request"]["prompt"] = "tampered"
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_E2E_PROVENANCE"):
            harness.validate_e2e_evidence(tampered_request)

        tampered_journal = evidence()
        tampered_journal["migration_journal"]["old_leader"]["session_id"] = "tampered"
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_E2E_JOURNAL_DIGEST"):
            harness.validate_e2e_evidence(tampered_journal)

    def test_duplicate_session_identity_and_missing_acknowledgement_fail_closed(self) -> None:
        duplicate = evidence()
        duplicate["identities"][1]["session_id"] = "session-leader"
        with self.assertRaises(core.ProtocolError):
            harness.validate_e2e_evidence(duplicate)

        missing_ack = evidence()
        missing_ack["acknowledgements"] = []
        with self.assertRaises(core.ProtocolError):
            harness.validate_e2e_evidence(missing_ack)

    def test_gate_tool_and_journal_trace_is_chronological_and_correlated(self) -> None:
        value = evidence()
        gate_records = value["gate_evidence"]["observed"]["probe_records"]
        tool_by_key = {item["request"]["operation_key"]: item for item in value["tool_events"]}
        journal_keys = {item["operation"] for item in value["migration_journal"]["steps"]}
        gate_keys = [item["operation_key"] for item in gate_records]
        self.assertTrue(set(gate_keys).issubset(tool_by_key))
        self.assertTrue(set(gate_keys).issubset(journal_keys))
        launch = next(
            item for item in value["tool_events"]
            if item["operation"] == "launchRole"
            and item["request"]["desired"].get("session_id") == "session-new"
        )
        pre = [tool_by_key[item["operation_key"]]["sequence"] for item in gate_records if item["gate_phase"] == "PRE_LAUNCH"]
        post = [tool_by_key[item["operation_key"]]["sequence"] for item in gate_records if item["gate_phase"] == "POST_LAUNCH"]
        self.assertLess(max(pre), launch["sequence"])
        self.assertGreater(min(post), launch["sequence"])
        validated = [item for item in value["migration_journal"]["steps"] if item["phase"] == "VALIDATED"]
        self.assertEqual(["BEFORE"] * 4 + ["APPLIED"] * 4, [item["status"] for item in validated])

    def test_e2e_rejects_early_launch_disjoint_keys_reordering_and_synthetic_steps(self) -> None:
        early = evidence()
        events = early["tool_events"]
        launch_index = next(i for i, item in enumerate(events) if item["operation"] == "launchRole" and item["request"]["desired"].get("session_id") == "session-new")
        launch = events.pop(launch_index)
        events.insert(0, launch)
        for sequence, item in enumerate(events, 1):
            item["sequence"] = sequence
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_CANDIDATE_LAUNCH|REORDERED_GATE_TRACE"):
            harness.validate_e2e_evidence(early)

        disjoint = evidence()
        key = disjoint["gate_evidence"]["observed"]["probe_records"][0]["operation_key"]
        event = next(item for item in disjoint["tool_events"] if item["request"]["operation_key"] == key)
        event["request"]["operation_key"] = "disjoint-operation-key"
        event["request_digest"] = core.digest(event["request"])
        with self.assertRaisesRegex(core.ProtocolError, "DISJOINT_GATE_TRACE"):
            harness.validate_e2e_evidence(disjoint)

        reordered = evidence()
        reordered["tool_events"][0], reordered["tool_events"][1] = reordered["tool_events"][1], reordered["tool_events"][0]
        for sequence, item in enumerate(reordered["tool_events"], 1):
            item["sequence"] = sequence
        with self.assertRaisesRegex(core.ProtocolError, "REORDERED_GATE_TRACE"):
            harness.validate_e2e_evidence(reordered)

        synthetic = evidence()
        original = next(step["operation"] for step in synthetic["migration_journal"]["steps"] if step["phase"] == "NEW_LEADER_REGISTERED")
        for step in synthetic["migration_journal"]["steps"]:
            if step["operation"] == original:
                step["operation"] = "synthetic-unobserved-step"
        synthetic["migration_journal_digest"] = core.digest(synthetic["migration_journal"])
        with self.assertRaisesRegex(core.ProtocolError, "SYNTHETIC_MIGRATION_STEP"):
            harness.validate_e2e_evidence(synthetic)

    def test_authority_transition_is_durable_and_cleanup_is_last(self) -> None:
        value = evidence()
        events = value["tool_events"]
        by_key = {item["request"]["operation_key"]: item for item in events}

        def phase_event(phase: str) -> dict:
            key = next(
                item["operation"] for item in value["migration_journal"]["steps"]
                if item["phase"] == phase and item["status"] == "APPLIED"
            )
            return by_key[key]

        registered = phase_event("NEW_LEADER_REGISTERED")
        activated = phase_event("STATE_ACTIVATED")
        workers_acked = phase_event("WORKERS_ACKED")
        old_keys = [
            item["operation"] for item in value["migration_journal"]["steps"]
            if item["phase"] == "OLD_SUPERSEDED" and item["status"] == "APPLIED"
        ]
        self.assertEqual(2, len(old_keys))
        final_reconcile, supersede = (by_key[key] for key in old_keys)
        committed = phase_event("COMMITTED")
        self.assertEqual("CANDIDATE", registered["response"]["authority_state"])
        self.assertFalse(any(
            item["sequence"] < activated["sequence"]
            and item["response"].get("binding_id") == "binding-leader-new"
            and item["response"].get("authority_state") == "ACTIVE"
            for item in events
        ))
        state = core.validate_role_state(activated["response"]["role_state"])
        self.assertEqual(2, state["leader_generation"])
        self.assertEqual("STATE_ACTIVATED", state["migration_phase"])
        self.assertEqual("binding-leader-new", state["roles"][0]["binding_id"])

        activation_envelopes = [
            item["envelope"] for item in value["envelopes"]
            if item["envelope"]["message_type"] == "LEADER_ACTIVATED"
        ]
        self.assertEqual({"executor", "reviewer"}, {item["recipient"]["role"] for item in activation_envelopes})
        ack_events = [item for item in events if item["response"].get("kind") == "ACTIVATION_ACK"]
        self.assertEqual({"executor", "reviewer"}, {item["response"]["role"] for item in ack_events})
        self.assertTrue(all(item["response"]["leader_generation"] == 2 for item in ack_events))
        self.assertTrue(all(item["response"]["parent_binding_id"] == "binding-leader-new" for item in ack_events))
        self.assertLess(max(item["sequence"] for item in ack_events), workers_acked["sequence"])
        self.assertEqual("reconcile", final_reconcile["operation"])
        self.assertEqual("SUPERSEDED", supersede["response"]["authority_state"])
        self.assertEqual("stopSession", committed["operation"])
        self.assertLess(workers_acked["sequence"], final_reconcile["sequence"])
        self.assertLess(final_reconcile["sequence"], supersede["sequence"])
        self.assertLess(supersede["sequence"], committed["sequence"])
        self.assertEqual(events[-1]["sequence"], committed["sequence"])
        self.assertEqual(
            ["reconcile", "bindRole", "stopSession"],
            [item["operation"] for item in events[-3:]],
        )
        for key, predicate in (
            (old_keys[0], "complete_generation_graph_with_old_fenced"),
            (old_keys[1], "old_binding_exactly_superseded"),
            (committed["request"]["operation_key"], "old_session_exactly_stopped"),
        ):
            pair = [item for item in value["migration_journal"]["steps"] if item["operation"] == key]
            self.assertEqual(["BEFORE", "APPLIED"], [item["status"] for item in pair])
            self.assertEqual(predicate, pair[0]["before"]["readback_predicate"])
            self.assertEqual(by_key[key]["response"], pair[1]["after"]["readback"])

    def test_authority_trace_rejects_early_active_missing_readback_bad_ack_and_early_cleanup(self) -> None:
        def refresh(item: dict) -> None:
            item["request_digest"] = core.digest(item["request"])
            item["response_digest"] = core.digest(item["response"])

        def resequence(events: list[dict]) -> None:
            for sequence, item in enumerate(events, 1):
                item["sequence"] = sequence

        early_active = evidence()
        registered_key = next(
            item["operation"] for item in early_active["migration_journal"]["steps"]
            if item["phase"] == "NEW_LEADER_REGISTERED" and item["status"] == "APPLIED"
        )
        registered = next(item for item in early_active["tool_events"] if item["request"]["operation_key"] == registered_key)
        registered["request"]["desired"]["authority_state"] = "ACTIVE"
        registered["response"]["authority_state"] = "ACTIVE"
        refresh(registered)
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_ACTIVE"):
            harness.validate_e2e_evidence(early_active)

        missing_readback = evidence()
        activation_key = next(
            item["operation"] for item in missing_readback["migration_journal"]["steps"]
            if item["phase"] == "STATE_ACTIVATED" and item["status"] == "APPLIED"
        )
        activation = next(item for item in missing_readback["tool_events"] if item["request"]["operation_key"] == activation_key)
        activation["request"]["desired"] = {}
        activation["response"] = {}
        refresh(activation)
        with self.assertRaisesRegex(core.ProtocolError, "MISSING_ROLE_STATE_READBACK"):
            harness.validate_e2e_evidence(missing_readback)

        bad_ack = evidence()
        ack = next(item for item in bad_ack["tool_events"] if item["response"].get("kind") == "ACTIVATION_ACK")
        ack["request"]["desired"]["parent_binding_id"] = "binding-leader-old"
        ack["response"]["parent_binding_id"] = "binding-leader-old"
        refresh(ack)
        with self.assertRaisesRegex(core.ProtocolError, "WORKER_ACTIVATION_READBACK_MISMATCH"):
            harness.validate_e2e_evidence(bad_ack)

        early_stop = evidence()
        events = early_stop["tool_events"]
        stop_index = next(i for i, item in enumerate(events) if item["operation"] == "stopSession" and item["response"].get("running") is False)
        stop = events.pop(stop_index)
        ack_index = next(i for i, item in enumerate(events) if item["response"].get("workers") == ["executor", "reviewer"])
        events.insert(ack_index, stop)
        resequence(events)
        with self.assertRaisesRegex(core.ProtocolError, "EARLY_OLD_STOP|INVALID_AUTHORITY_TRACE"):
            harness.validate_e2e_evidence(early_stop)

        early_reconcile = evidence()
        events = early_reconcile["tool_events"]
        commit_key = next(
            item["operation"] for item in early_reconcile["migration_journal"]["steps"]
            if item["phase"] == "OLD_SUPERSEDED" and item["status"] == "APPLIED"
            and next(
                event for event in early_reconcile["tool_events"]
                if event["request"]["operation_key"] == item["operation"]
            )["operation"] == "reconcile"
        )
        commit_index = next(i for i, item in enumerate(events) if item["request"]["operation_key"] == commit_key)
        commit = events.pop(commit_index)
        ack_index = next(i for i, item in enumerate(events) if item["response"].get("workers") == ["executor", "reviewer"])
        events.insert(ack_index, commit)
        resequence(events)
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_AUTHORITY_TRACE|EARLY_FINAL_RECONCILE"):
            harness.validate_e2e_evidence(early_reconcile)

    def test_final_effect_evidence_rejects_missing_miskeyed_ambiguous_and_reordered_records(self) -> None:
        def final_keys(value: dict) -> tuple[str, str, str]:
            old = [
                step["operation"] for step in value["migration_journal"]["steps"]
                if step["phase"] == "OLD_SUPERSEDED" and step["status"] == "APPLIED"
            ]
            committed = next(
                step["operation"] for step in value["migration_journal"]["steps"]
                if step["phase"] == "COMMITTED" and step["status"] == "APPLIED"
            )
            return old[0], old[1], committed

        missing = evidence()
        _, supersede_key, _ = final_keys(missing)
        missing["migration_journal"]["steps"] = [
            step for step in missing["migration_journal"]["steps"]
            if not (step["operation"] == supersede_key and step["status"] == "APPLIED")
        ]
        for sequence, step in enumerate(missing["migration_journal"]["steps"], 1):
            step["sequence"] = sequence
        missing["migration_journal_digest"] = core.digest(missing["migration_journal"])
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_FINAL_EFFECT_JOURNAL|INVALID_AUTHORITY_TRACE"):
            harness.validate_e2e_evidence(missing)

        miskeyed = evidence()
        final_key, _, _ = final_keys(miskeyed)
        for step in miskeyed["migration_journal"]["steps"]:
            if step["operation"] == final_key:
                step["operation"] = "fabricated-final-key"
        miskeyed["migration_journal_digest"] = core.digest(miskeyed["migration_journal"])
        with self.assertRaisesRegex(core.ProtocolError, "SYNTHETIC_MIGRATION_STEP|MISKEYED_FINAL_EFFECT|INVALID_APPLIED_CORRELATION"):
            harness.validate_e2e_evidence(miskeyed)

        ambiguous = evidence()
        final_key, _, _ = final_keys(ambiguous)
        final_event = next(
            event for event in ambiguous["tool_events"]
            if event["request"]["operation_key"] == final_key
        )
        final_event["response"].pop("final_reconciled")
        final_event["response_digest"] = core.digest(final_event["response"])
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_FINAL_EFFECT_JOURNAL|INVALID_FINAL_RECONCILE_READBACK"):
            harness.validate_e2e_evidence(ambiguous)

        reordered = evidence()
        final_key, supersede_key, _ = final_keys(reordered)
        events = reordered["tool_events"]
        final_index = next(i for i, event in enumerate(events) if event["request"]["operation_key"] == final_key)
        supersede_index = next(i for i, event in enumerate(events) if event["request"]["operation_key"] == supersede_key)
        events[final_index], events[supersede_index] = events[supersede_index], events[final_index]
        for sequence, event in enumerate(events, 1):
            event["sequence"] = sequence
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_AUTHORITY_TRACE|EARLY_OLD_STOP"):
            harness.validate_e2e_evidence(reordered)


class PreAuthorityLiveSmokeTests(unittest.TestCase):
    def fixture(self) -> dict:
        artifact = json.loads(COMPATIBILITY_PATH.read_text(encoding="utf-8"))
        identities = [
            {"role": "leader", "cli_tool": "codex", "session_id": "session-leader", "binding_id": "binding-leader"},
            {"role": "executor", "cli_tool": "codex", "session_id": "session-executor", "binding_id": "binding-executor"},
            {"role": "reviewer", "cli_tool": "codex", "session_id": "session-reviewer", "binding_id": "binding-reviewer"},
        ]
        context = {
            "run_id": "run-1", "migration_id": "run-1-pre-authority",
            "source_generation": 1, "coordinator_binding_id": "binding-leader",
            "coordinator_session_id": "session-leader",
            "evidence_started_at": "2026-07-26T12:00:00Z",
            "evidence_expires_at": "2026-07-26T13:00:00Z",
        }
        by_role = {item["role"]: item for item in identities}
        records = []
        for sequence, (probe_id, primitive, owner_role, target_role, predicate) in enumerate(harness.SUPPORTED_PRE_AUTHORITY_PLAN, 1):
            operation_key = core.operation_key(context["migration_id"], probe_id, primitive)
            if primitive == "resolveProject":
                request = response = {"project_root": str(ROOT.resolve())}
            elif primitive == "observeSession":
                request = {"session_id": by_role[target_role]["session_id"]}
                response = copy.deepcopy(by_role[target_role])
            elif primitive == "reconcile":
                request = {"binding_ids": [item["binding_id"] for item in identities]}
                response = {"bindings": [{"binding_id": item["binding_id"], "parent_binding_id": None if item["role"] == "leader" else "binding-leader"} for item in identities]}
            elif primitive == "updateMilestone":
                request = response = {"binding_id": "binding-executor", "status": "running", "progress": 20}
            else:
                request = {"leader_binding_id": "binding-leader", "executor_binding_id": "binding-executor", "status": "iteration-3-fixing"}
                response = {"operation_key": operation_key, "queued": True}
            readback = {"predicate": predicate, "outcome": "COMPLETE", "value": copy.deepcopy(response)}
            records.append({
                "sequence": sequence, "primitive": primitive, "probe_id": probe_id,
                "operation_key": operation_key,
                "owner": {"binding_id": by_role[owner_role]["binding_id"], "session_id": by_role[owner_role]["session_id"]},
                "target": {"binding_id": by_role[target_role]["binding_id"], "session_id": by_role[target_role]["session_id"]},
                "request": copy.deepcopy(request), "response": copy.deepcopy(response), "readback": readback,
                "hashes": {"request_sha256": core.digest(request), "response_sha256": core.digest(response), "readback_sha256": core.digest(readback["value"])},
                "outcome": "SUCCEEDED", "recorded_at": f"2026-07-26T12:01:{sequence:02d}Z",
            })
        return harness.build_pre_authority_live_smoke(
            run_id="run-1", project_root=ROOT, compatibility_artifact=artifact,
            compatibility_row_id="windows-local-claude-2.1.220-codex-0.144.6",
            identities=identities, probe_context=context, probe_records=records,
            captured_at="2026-07-26T12:02:00Z",
            provenance={"kind": "ccpanes-live-capture", "role_process_count": 3},
        )

    def test_live_smoke_round_trip_preserves_deferred_authority(self) -> None:
        value = self.fixture()
        self.assertEqual("PRE_AUTHORITY_OBSERVED", value["status"])
        self.assertTrue(all(item == "DEFERRED_POST_PASS" for item in value["authority_boundaries"].values()))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "live.json"
            expected = harness.write_pre_authority_live_smoke(path, value)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertEqual(value, harness.validate_pre_authority_live_smoke(json.loads(path.read_text(encoding="utf-8"))))

    def test_live_smoke_rejects_fabricated_pass_and_bad_correlation(self) -> None:
        fabricated = self.fixture()
        fabricated["probe_records"][0]["passed"] = True
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_LIVE_SMOKE_PROBE"):
            harness.validate_pre_authority_live_smoke(fabricated)

        wrong_target = self.fixture()
        wrong_target["probe_records"][0]["target"]["session_id"] = "session-reviewer"
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_LIVE_SMOKE_CORRELATION"):
            harness.validate_pre_authority_live_smoke(wrong_target)

        premature = self.fixture()
        premature["authority_boundaries"]["state_activation"] = "PASS"
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_LIVE_SMOKE_BOUNDARY"):
            harness.validate_pre_authority_live_smoke(premature)

    def test_live_smoke_requires_exact_supported_row_order_and_values(self) -> None:
        for mutation in ("missing", "duplicate", "extra", "reordered"):
            invalid = self.fixture()
            if mutation == "missing":
                invalid["probe_records"].pop()
            elif mutation == "duplicate":
                invalid["probe_records"][-1] = copy.deepcopy(invalid["probe_records"][-2])
            elif mutation == "extra":
                invalid["probe_records"].append(copy.deepcopy(invalid["probe_records"][-1]))
            else:
                invalid["probe_records"][0], invalid["probe_records"][1] = invalid["probe_records"][1], invalid["probe_records"][0]
            with self.subTest(mutation=mutation), self.assertRaises(core.ProtocolError):
                harness.validate_pre_authority_live_smoke(invalid)

        mutations = (
            ("identity", lambda item: item["target"].__setitem__("session_id", "session-reviewer")),
            ("key", lambda item: item.__setitem__("operation_key", "wrong-key")),
            ("hash", lambda item: item["hashes"].__setitem__("response_sha256", SHA_A)),
            ("outcome", lambda item: item.__setitem__("outcome", "FAILED")),
        )
        for name, mutate in mutations:
            invalid = self.fixture()
            mutate(invalid["probe_records"][0])
            with self.subTest(name=name), self.assertRaises(core.ProtocolError):
                harness.validate_pre_authority_live_smoke(invalid)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-team-workflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import protocol_core as core  # noqa: E402


NOW = "2026-07-26T12:00:00Z"
LATER = "2026-07-26T12:05:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def party(role: str, cli: str, session: str, binding: str) -> dict:
    return {
        "role": role,
        "cli_tool": cli,
        "session_id": session,
        "binding_id": binding,
    }


def descriptor(role: str, generation: int = 1, cli: str = "codex") -> dict:
    leader_binding = "binding-leader"
    return {
        "schema_version": 1,
        "role": role,
        "cli_tool": cli,
        "adapter_id": f"{cli}-ccpanes",
        "runtime_kind": "local",
        "project_root": r"E:\meizhouyu\agentstudy\agent-team-workflow",
        "session_id": f"session-{role}",
        "resume_id": None,
        "binding_id": f"binding-{role}",
        "parent_binding_id": None if role == "leader" else leader_binding,
        "leader_generation": generation,
        "authority_state": "ACTIVE" if role == "leader" else "FROZEN",
        "required_capabilities": ["observeSession"],
        "advertised_capabilities": ["observeSession", "reconcile"],
        "verified_at": NOW,
    }


def role_state(generation: int = 1) -> dict:
    return {
        "schema_version": 1,
        "workflow_version": "0.2.0",
        "run_id": "run-1",
        "leader_generation": generation,
        "migration_id": None,
        "migration_phase": None,
        "roles": [descriptor(role, generation) for role in core.ROLES],
    }


def envelope(
    message_type: str = "HANDSHAKE",
    *,
    sender: dict | None = None,
    recipient: dict | None = None,
    payload: object | None = None,
    scope: str = "ROLE",
    message_id: str = "message-1",
    reply_to: str | None = None,
    migration_id: str | None = None,
    generation: int = 1,
    nonce: str = "nonce-1",
) -> dict:
    if payload is None:
        payload = {
            "probe_kind": "CROSS_CLI_HANDSHAKE",
            "run_id": "run-1",
            "nonce": nonce,
        }
    value = {
        "schema_version": 1,
        "authority_scope": scope,
        "message_id": message_id,
        "reply_to": reply_to,
        "run_id": "run-1",
        "migration_id": migration_id,
        "leader_generation": generation,
        "message_type": message_type,
        "sender": sender or party("leader", "claude", "session-leader", "binding-leader"),
        "recipient": recipient or party("executor", "codex", "session-executor", "binding-executor"),
        "nonce": nonce,
        "created_at": NOW,
        "expires_at": LATER,
        "payload": payload,
        "payload_path": None,
        "payload_sha256": core.digest(payload),
    }
    return value


def candidate_context() -> dict:
    return {
        "migration_id": "migration-1",
        "source_generation": 1,
        "compatibility_digest": SHA_A,
        "old_leader": party("leader", "codex", "old-session", "old-binding"),
        "candidate": {
            "role": "leader",
            "cli_tool": "claude",
            "session_id": "candidate-session",
            "binding_id": "candidate-binding",
            "resume_lineage": "candidate-lineage",
        },
    }


def candidate_messages() -> tuple[list[dict], dict]:
    context = candidate_context()
    old = context["old_leader"]
    candidate = party("leader", "claude", "candidate-session", "candidate-binding")
    handshake_payload = {
        "probe_id": "probe-main",
        "target_generation": 2,
        "compatibility_row_id": "windows-local-claude-2.1.220-codex-0.144.6",
        "compatibility_digest": SHA_A,
        "candidate_binding_id": "candidate-binding",
        "candidate_session_id": "candidate-session",
        "candidate_resume_lineage": "candidate-lineage",
        "handoff_digest": SHA_B,
        "ordered_probe_ids": ["prompt", "binding", "report"],
        "operation_keys": {
            "prompt": "migration-1:probe-main:prompt",
            "binding": "migration-1:probe-main:binding",
            "report": "migration-1:probe-main:report",
        },
    }
    handshake = envelope(
        sender=old,
        recipient=candidate,
        payload=handshake_payload,
        scope="CANDIDATE_VALIDATION",
        migration_id="migration-1",
    )
    request_hash = core.envelope_request_hash(handshake)
    echo = {
        "original_message_id": "message-1",
        "original_nonce": "nonce-1",
        "canonical_request_hash": request_hash,
        "migration_id": "migration-1",
        "probe_id": "probe-main",
    }
    receipt = envelope(
        "RECEIPT_ACK",
        sender=candidate,
        recipient=old,
        payload={
            **echo,
            "candidate_identity": candidate,
        },
        scope="CANDIDATE_VALIDATION",
        message_id="message-2",
        reply_to="message-1",
        migration_id="migration-1",
    )
    probes = [
        {
            "probe_id": probe_id,
            "operation_key": handshake_payload["operation_keys"][probe_id],
            "effect_policy": "RECONCILABLE_IDEMPOTENT",
            "terminal_outcome": "SUCCEEDED",
            "evidence_digest": str(index) * 64,
        }
        for index, probe_id in enumerate(handshake_payload["ordered_probe_ids"], 1)
    ]
    completion = envelope(
        "COMPLETION",
        sender=candidate,
        recipient=old,
        payload={
            **echo,
            "probes": probes,
            "candidate_binding_readback": {"binding_id": "candidate-binding"},
            "report_delivery_readback": {"operation_key": "migration-1:probe-main:report"},
        },
        scope="CANDIDATE_VALIDATION",
        message_id="message-3",
        reply_to="message-1",
        migration_id="migration-1",
    )
    return [handshake, receipt, completion], context


def append_candidate_terminal(
    ledger: core.CandidateLedger, message: dict, context: dict,
) -> None:
    ledger.append_envelope(message, "RECEIVED", context, recorded_at=NOW)
    ledger.append_envelope(message, "EXECUTING", context, recorded_at=LATER)
    ledger.append_envelope(message, "SUCCEEDED", context, recorded_at=LATER)


def migration(phase: str = "PREPARED") -> dict:
    nullable = phase in ("CREATED", "PREFLIGHTED", "ABORTED")
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "migration_id": "migration-1",
        "phase": phase,
        "source_generation": 1,
        "target_generation": 2,
        "old_leader": {"binding_id": "old-binding", "session_id": "old-session"},
        "candidate": {
            "binding_id": None if nullable else "candidate-binding",
            "active_session_id": None if nullable else "candidate-session",
            "resume_lineage": None if nullable else "candidate-lineage",
        },
        "compatibility_row_id": "windows-local-claude-2.1.220-codex-0.144.6",
        "compatibility_digest": SHA_A,
        "before_state": {"leader": "old-binding"},
        "expected_binding_parents": {
            "binding-executor": "old-binding",
            "binding-reviewer": "old-binding",
        },
        "observed_binding_parents": {
            "binding-executor": "old-binding",
            "binding-reviewer": "old-binding",
        },
        "steps": [],
        "recovery_decision": None,
        "candidate_ledger_digest": None,
        "candidate_ledger_closed": False,
    }


class CanonicalAndRoleStateTests(unittest.TestCase):
    def test_canonical_json_and_timestamp_are_deterministic(self) -> None:
        left = {"z": 1, "a": {"b": "\N{SNOWMAN}", "a": [2, 1]}}
        right = {"a": {"a": [2, 1], "b": "\N{SNOWMAN}"}, "z": 1}
        self.assertEqual(core.canonical_bytes(left), core.canonical_bytes(right))
        self.assertEqual(b'{"a":{"a":[2,1],"b":"\xe2\x98\x83"},"z":1}', core.canonical_bytes(left))
        self.assertEqual("2026-07-26T12:00:00Z", core.normalize_timestamp("2026-07-26T20:00:00+08:00"))

    def test_role_state_requires_exact_order_count_and_authority(self) -> None:
        valid = role_state()
        self.assertEqual(valid, core.validate_role_state(valid))
        for mutate in (
            lambda state: state["roles"].pop(),
            lambda state: state["roles"].reverse(),
            lambda state: state["roles"][1].__setitem__("binding_id", "binding-leader"),
            lambda state: state["roles"][1].__setitem__("session_id", "session-leader"),
            lambda state: state["roles"][0].__setitem__("authority_state", "FROZEN"),
        ):
            invalid = copy.deepcopy(valid)
            mutate(invalid)
            with self.subTest(invalid=invalid):
                with self.assertRaises(core.ProtocolError):
                    core.validate_role_state(invalid)

    def test_atomic_backup_recovery_is_validated_and_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "roles.json"
            first = role_state()
            core.write_role_state(path, first)
            second = copy.deepcopy(first)
            second["roles"][0]["verified_at"] = "2026-07-26T12:01:00Z"
            core.write_role_state(path, second, expected_generation=1)
            path.write_text('{"schema_version":1', encoding="utf-8")
            recovered = core.recover_role_state(path, lambda observed: observed == first)
            self.assertEqual(first, recovered)
            self.assertEqual(first, json.loads(path.read_text(encoding="utf-8")))
            path.write_text("corrupt", encoding="utf-8")
            with self.assertRaisesRegex(core.ProtocolError, "RECONCILIATION_FAILED"):
                core.recover_role_state(path, lambda _observed: False)

    def test_schema0_upgrade_requires_exact_three_live_codex_roles_once(self) -> None:
        proposed = role_state()
        live = [
            {key: value for key, value in item.items() if key in {
                "role", "cli_tool", "session_id",
                "binding_id", "parent_binding_id", "project_root",
            }}
            for item in proposed["roles"]
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "leader-state.md"
            target = root / "roles.json"
            legacy.write_text("---\nrun_id: run-1\nleader_generation: 1\n---\n", encoding="utf-8")
            core.upgrade_schema0(target, legacy, proposed, live)
            self.assertEqual(proposed, core.validate_role_state(core.load_json(target)))
            with self.assertRaisesRegex(core.ProtocolError, "UPGRADE_ALREADY_ATTEMPTED"):
                core.upgrade_schema0(target, legacy, proposed, live)

            target.unlink()
            wrong = copy.deepcopy(live)
            wrong[1]["session_id"] = "other-session"
            with self.assertRaisesRegex(core.ProtocolError, "LIVE_VALIDATION_FAILED"):
                core.upgrade_schema0(target, legacy, proposed, wrong)

            target.write_text("partial", encoding="utf-8")
            with self.assertRaisesRegex(core.ProtocolError, "UPGRADE_ALREADY_ATTEMPTED"):
                core.upgrade_schema0(target, legacy, proposed, live)


class EnvelopeTests(unittest.TestCase):
    def runtime(self, request: dict) -> dict:
        return {
            "run_id": "run-1",
            "leader_generation": 1,
            "now": "2026-07-26T12:01:00Z",
            "recipient": request["recipient"],
            "allowed_senders": [request["sender"]["binding_id"]],
            "predecessors": {},
        }

    def test_role_cross_cli_handshake_and_exact_echo(self) -> None:
        request = envelope()
        core.validate_envelope(request, runtime_context=self.runtime(request))
        reply_payload = {
            "probe_kind": "CROSS_CLI_HANDSHAKE",
            "run_id": "run-1",
            "nonce": "nonce-1",
            "original_message_id": "message-1",
            "canonical_request_hash": core.envelope_request_hash(request),
        }
        reply = envelope(
            "RECEIPT_ACK",
            sender=request["recipient"],
            recipient=request["sender"],
            payload=reply_payload,
            message_id="message-2",
            reply_to="message-1",
        )
        core.validate_cross_cli_handshake_reply(request, reply)
        reply["payload"]["nonce"] = "tampered"
        reply["payload_sha256"] = core.digest(reply["payload"])
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_ECHO"):
            core.validate_cross_cli_handshake_reply(request, reply)

    def test_runtime_rejects_expired_stale_and_wrong_recipient(self) -> None:
        request = envelope()
        cases = {
            "EXPIRED": {"now": LATER},
            "STALE_GENERATION": {"leader_generation": 2},
            "WRONG_RECIPIENT": {"recipient": party("reviewer", "codex", "x", "y")},
        }
        for code, change in cases.items():
            runtime = self.runtime(request)
            runtime.update(change)
            with self.subTest(code=code):
                with self.assertRaisesRegex(core.ProtocolError, code):
                    core.validate_envelope(request, runtime_context=runtime)

    def test_every_role_message_payload_is_exact_and_versioned(self) -> None:
        payloads = {
            "ASSIGNMENT": ({
                "spec_path": ".codex/spec.md", "spec_sha256": SHA_A,
                "spec_version": 8, "leader_binding_id": "binding-leader",
                "executor_binding_id": "binding-executor",
                "required_skill": "orchestrate-agent-team",
            }, None, None, 1),
            "RECEIPT_ACK": ({
                "original_message_id": "assignment-1", "original_nonce": "nonce-0",
                "canonical_request_hash": SHA_A, "accepted": True,
                "operation_key": "receipt:assignment-1",
            }, "assignment-1", None, 1),
            "COMPLETION": ({
                "original_message_id": "assignment-1", "original_nonce": "nonce-0",
                "canonical_request_hash": SHA_A, "status": "SUCCEEDED",
                "result_path": ".codex/plan.md", "result_sha256": SHA_B,
            }, "assignment-1", None, 1),
            "REVIEW_REQUEST": ({
                "plan_path": ".codex/plan.md", "review_iteration": 1,
                "review_fingerprint": SHA_A, "review_scope": ["."],
            }, None, None, 1),
            "REVIEW_VERDICT": ({
                "original_message_id": "review-1", "original_nonce": "nonce-0",
                "canonical_request_hash": SHA_A, "verdict": "PASS",
                "review_path": ".codex/review.md", "review_sha256": SHA_B,
            }, "review-1", None, 1),
            "LEADER_ACTIVATED": ({
                "migration_id": "migration-1", "leader_generation": 2,
                "leader_binding_id": "binding-leader-new",
                "executor_parent_binding_id": "binding-leader-new",
                "reviewer_parent_binding_id": "binding-leader-new",
                "roles_sha256": SHA_A,
            }, None, "migration-1", 2),
        }
        for message_type, (payload, reply_to, migration_id, generation) in payloads.items():
            value = envelope(
                message_type, payload=payload, message_id=message_type.lower(),
                reply_to=reply_to, migration_id=migration_id, generation=generation,
            )
            with self.subTest(message_type=message_type):
                core.validate_envelope(value)
                invalid = copy.deepcopy(value)
                invalid["payload"]["extra"] = True
                invalid["payload_sha256"] = core.digest(invalid["payload"])
                with self.assertRaisesRegex(core.ProtocolError, "EXTRA_FIELD"):
                    core.validate_envelope(invalid)

    def test_durable_payload_path_is_decoded_and_confined(self) -> None:
        assignment = {
            "spec_path": ".codex/spec.md", "spec_sha256": SHA_A,
            "spec_version": 8, "leader_binding_id": "binding-leader",
            "executor_binding_id": "binding-executor",
            "required_skill": "orchestrate-agent-team",
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload_dir = root / ".codex" / "messages" / "payloads"
            payload_dir.mkdir(parents=True)
            target = payload_dir / "assignment.json"
            target.write_bytes(core.canonical_bytes(assignment))
            value = envelope("ASSIGNMENT", payload=assignment)
            value.update({
                "payload": None,
                "payload_path": ".codex/messages/payloads/assignment.json",
                "payload_sha256": core.digest(target.read_bytes()),
            })
            core.validate_envelope(value, project_root=root)

            outside = root / ".codex" / "outside.json"
            outside.write_bytes(core.canonical_bytes(assignment))
            escaped = copy.deepcopy(value)
            escaped["payload_path"] = ".codex/messages/payloads/../../outside.json"
            escaped["payload_sha256"] = core.digest(outside.read_bytes())
            with self.assertRaisesRegex(core.ProtocolError, "PATH_TRAVERSAL"):
                core.validate_envelope(escaped, project_root=root)

    def test_durable_payload_rejects_file_parent_and_external_symlinks(self) -> None:
        assignment = {
            "spec_path": ".codex/spec.md", "spec_sha256": SHA_A,
            "spec_version": 8, "leader_binding_id": "binding-leader",
            "executor_binding_id": "binding-executor",
            "required_skill": "orchestrate-agent-team",
        }
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as external_raw:
            root = Path(raw)
            payload_dir = root / ".codex" / "messages" / "payloads"
            payload_dir.mkdir(parents=True)
            target = payload_dir / "assignment.json"
            target.write_bytes(core.canonical_bytes(assignment))
            value = envelope("ASSIGNMENT", payload=assignment)
            value.update({"payload": None, "payload_sha256": core.digest(target.read_bytes())})

            links: list[tuple[Path, Path, bool, str]] = [
                (payload_dir / "file-link.json", target, False, ".codex/messages/payloads/file-link.json"),
            ]
            real_parent = payload_dir / "real-parent"
            real_parent.mkdir()
            (real_parent / "assignment.json").write_bytes(target.read_bytes())
            links.append((payload_dir / "parent-link", real_parent, True, ".codex/messages/payloads/parent-link/assignment.json"))
            external = Path(external_raw) / "external.json"
            external.write_bytes(target.read_bytes())
            links.append((payload_dir / "external-link.json", external, False, ".codex/messages/payloads/external-link.json"))

            created = 0
            for link, destination, is_directory, relative in links:
                try:
                    os.symlink(destination, link, target_is_directory=is_directory)
                except (OSError, NotImplementedError):
                    continue
                created += 1
                candidate = copy.deepcopy(value)
                candidate["payload_path"] = relative
                with self.subTest(relative=relative):
                    with self.assertRaisesRegex(core.ProtocolError, "SYMLINK_REJECTED"):
                        core.validate_envelope(candidate, project_root=root)
            if created != len(links):
                self.skipTest("host cannot create every required file/directory symlink")

    def test_durable_payload_digest_and_json_decode_share_one_captured_buffer(self) -> None:
        assignment = {
            "spec_path": ".codex/spec.md", "spec_sha256": SHA_A,
            "spec_version": 8, "leader_binding_id": "binding-leader",
            "executor_binding_id": "binding-executor",
            "required_skill": "orchestrate-agent-team",
        }
        replacement = {**assignment, "unexpected": "swapped-after-secure-read"}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / ".codex" / "messages" / "payloads" / "assignment.json"
            target.parent.mkdir(parents=True)
            original = core.canonical_bytes(assignment)
            target.write_bytes(original)
            value = envelope("ASSIGNMENT", payload=assignment)
            value.update({
                "payload": None,
                "payload_path": ".codex/messages/payloads/assignment.json",
                "payload_sha256": core.digest(original),
            })
            secure_read = core._secure_read_payload_bytes

            def capture_then_swap(project_root: Path, relative: str) -> bytes:
                captured = secure_read(project_root, relative)
                target.write_bytes(core.canonical_bytes(replacement))
                return captured

            with mock.patch.object(core, "_secure_read_payload_bytes", side_effect=capture_then_swap):
                core.validate_envelope(value, project_root=root)
            self.assertEqual(core.canonical_bytes(replacement), target.read_bytes())

    def test_durable_payload_deterministically_rejects_reparse_components(self) -> None:
        assignment = {
            "spec_path": ".codex/spec.md", "spec_sha256": SHA_A,
            "spec_version": 8, "leader_binding_id": "binding-leader",
            "executor_binding_id": "binding-executor",
            "required_skill": "orchestrate-agent-team",
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload_dir = root / ".codex" / "messages" / "payloads"
            payload_dir.mkdir(parents=True)
            value = envelope("ASSIGNMENT", payload=assignment)
            value.update({"payload": None, "payload_sha256": core.digest(core.canonical_bytes(assignment))})
            cases = (
                (payload_dir / "file-link.json", ".codex/messages/payloads/file-link.json"),
                (payload_dir / "parent-link", ".codex/messages/payloads/parent-link/assignment.json"),
                (payload_dir / "external-link.json", ".codex/messages/payloads/external-link.json"),
            )
            for rejected, relative in cases:
                candidate = copy.deepcopy(value)
                candidate["payload_path"] = relative
                with self.subTest(relative=relative), mock.patch.object(
                    core, "_is_reparse_or_symlink", side_effect=lambda path, rejected=rejected: path == rejected,
                ):
                    with self.assertRaisesRegex(core.ProtocolError, "SYMLINK_REJECTED"):
                        core.validate_envelope(candidate, project_root=root)

    def test_candidate_sequence_is_strictly_ordered_and_fenced(self) -> None:
        messages, context = candidate_messages()
        core.validate_candidate_sequence(messages, context)
        mutations = []
        mutations.append(lambda values: values.reverse())
        mutations.append(lambda values: values[1]["payload"].__setitem__("original_nonce", "wrong"))
        mutations.append(lambda values: values[2]["payload"]["probes"].reverse())
        mutations.append(lambda values: values[0]["payload"].__setitem__("candidate_binding_id", "wrong"))
        mutations.append(lambda values: values[2]["payload"].__setitem__("extra", True))
        mutations.append(lambda values: values[1].__setitem__("migration_id", "wrong-migration"))
        mutations.append(lambda values: values[2].__setitem__("leader_generation", 2))
        for mutate in mutations:
            invalid = copy.deepcopy(messages)
            mutate(invalid)
            for item in invalid:
                item["payload_sha256"] = core.digest(item["payload"])
            with self.subTest(mutate=mutate):
                with self.assertRaises(core.ProtocolError):
                    core.validate_candidate_sequence(invalid, context)

    def test_candidate_scope_rejects_normal_command_type(self) -> None:
        invalid = envelope(
            "ASSIGNMENT",
            scope="CANDIDATE_VALIDATION",
            migration_id="migration-1",
            payload={"work": "forbidden"},
        )
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_CANDIDATE_MESSAGE"):
            core.validate_envelope(invalid, candidate_context=candidate_context())


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "executor.jsonl"
        self.state = role_state()
        self.ledger = core.Ledger(
            self.path,
            lambda: self.state,
            "executor",
            "binding-executor",
            "session-executor",
            1,
        )
        self.key = ("binding-leader", "run-1", 1, "message-1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append_flow(self) -> None:
        self.ledger.append(self.key, SHA_A, "RECEIVED", recorded_at=NOW)
        self.ledger.append(
            self.key,
            SHA_A,
            "EXECUTING",
            operation_key="operation-1",
            effect_policy=core.EffectPolicy.RECONCILABLE_IDEMPOTENT,
            readback_predicate="fake_effect_has_key",
            recorded_at="2026-07-26T12:00:01Z",
        )

    def test_hash_chain_replay_restart_and_same_key_conflict(self) -> None:
        self.append_flow()
        terminal = self.ledger.append(
            self.key, SHA_A, "SUCCEEDED", outcome={"value": 1},
            recorded_at="2026-07-26T12:00:02Z",
        )
        restarted = core.Ledger(
            self.path, lambda: self.state, "executor",
            "binding-executor", "session-executor", 1,
        )
        replay = restarted.append(self.key, SHA_A, "RECEIVED", recorded_at=LATER)
        self.assertEqual(terminal, replay)
        self.assertEqual(3, len(restarted.records()))
        with self.assertRaisesRegex(core.ProtocolError, "REQUEST_HASH_CONFLICT"):
            restarted.append(self.key, SHA_B, "RECEIVED", recorded_at=LATER)

    def test_torn_tail_is_quarantined_but_interior_corruption_fails_closed(self) -> None:
        self.ledger.append(self.key, SHA_A, "RECEIVED", recorded_at=NOW)
        valid = self.path.read_bytes()
        self.path.write_bytes(valid + b'{"partial"')
        recovered = core.read_ledger(self.path)
        self.assertEqual(1, len(recovered))
        quarantines = list(self.path.parent.glob(self.path.name + ".torn.*.quarantine"))
        self.assertEqual(1, len(quarantines))
        self.path.write_bytes(b"not-json\n" + valid)
        with self.assertRaisesRegex(core.ProtocolError, "LEDGER_CORRUPT"):
            core.read_ledger(self.path)

    def test_stale_writer_and_generation_are_fenced(self) -> None:
        self.state["roles"][1]["session_id"] = "replacement-session"
        with self.assertRaisesRegex(core.ProtocolError, "STALE_WRITER"):
            self.ledger.append(self.key, SHA_A, "RECEIVED", recorded_at=NOW)

    def test_effect_crash_reconciliation_outcomes(self) -> None:
        calls: list[str] = []
        execute = lambda: calls.append("effect") or {"executed": True}
        cases = (
            (core.EffectPolicy.PURE, core.Observation.UNKNOWN, False, "SUCCEEDED", 1),
            (core.EffectPolicy.RECONCILABLE_IDEMPOTENT, core.Observation.COMPLETE, True, "SUCCEEDED", 0),
            (core.EffectPolicy.RECONCILABLE_IDEMPOTENT, core.Observation.ABSENT, True, "SUCCEEDED", 1),
            (core.EffectPolicy.RECONCILABLE_IDEMPOTENT, core.Observation.UNKNOWN, True, "INDETERMINATE", 0),
            (core.EffectPolicy.UNRECONCILABLE, core.Observation.ABSENT, True, "INDETERMINATE", 0),
            (core.EffectPolicy.RECONCILABLE_IDEMPOTENT, core.Observation.ABSENT, False, "INDETERMINATE", 0),
        )
        for policy, observation, honors, expected, call_count in cases:
            calls.clear()
            outcome, _details = core.reconcile_unresolved(
                policy, observation, adapter_honors_key=honors, execute=execute,
            )
            with self.subTest(policy=policy, observation=observation, honors=honors):
                self.assertEqual(expected, outcome)
                self.assertEqual(call_count, len(calls))


class MigrationTests(unittest.TestCase):
    def test_journal_phase_order_and_recovery_direction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            journal.write(migration("PREPARED"))
            self.assertEqual("ROLL_BACK", journal.recovery_direction())
            journal.record_step(
                "VALIDATED", "validate-candidate", "BEFORE", {}, None, NOW,
            )
            self.assertEqual("PREPARED", journal.load()["phase"])
            journal.record_step(
                "VALIDATED", "validate-candidate", "APPLIED", None, {"valid": True}, NOW,
            )
            self.assertEqual("VALIDATED", journal.load()["phase"])
            regressed = journal.load()
            regressed["phase"] = "PREFLIGHTED"
            with self.assertRaisesRegex(core.ProtocolError, "PHASE_REGRESSION"):
                journal.write(regressed)

            journal.record_step(
                "STATE_ACTIVATED", "activate-role-state", "BEFORE",
                {"leader_generation": 1}, None, NOW,
            )
            self.assertEqual("VALIDATED", journal.load()["phase"])
            self.assertEqual("ROLL_BACK", journal.recovery_direction())
            journal.record_step(
                "STATE_ACTIVATED", "activate-role-state", "APPLIED", None,
                {"leader_generation": 2}, LATER,
            )
            self.assertEqual("ROLL_FORWARD", journal.recovery_direction())
            rollback = journal.load()
            rollback["phase"] = "ROLLED_BACK"
            rollback["candidate_ledger_digest"] = "0" * 64
            rollback["candidate_ledger_closed"] = True
            rollback["recovery_decision"] = "ROLL_BACK"
            with self.assertRaisesRegex(core.ProtocolError, "ROLLBACK_AFTER_ACTIVATION"):
                journal.write(rollback)

    def test_v9_multi_effect_phase_advances_only_after_supersede(self) -> None:
        self.assertLess(
            core.PHASE_ORDER["WORKERS_ACKED"],
            core.PHASE_ORDER["OLD_SUPERSEDED"],
        )
        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            journal.write(migration("WORKERS_ACKED"))
            reconcile_key = core.operation_key("migration-1", "commit-readback", "reconcile")
            supersede_key = core.operation_key("migration-1", "supersede-old", "bindRole")
            journal.record_step(
                "OLD_SUPERSEDED", reconcile_key, "BEFORE", {"operation_key": reconcile_key},
                None, NOW, advance_phase=False,
            )
            journal.record_step(
                "OLD_SUPERSEDED", reconcile_key, "APPLIED", None,
                {"operation_key": reconcile_key, "readback": {"complete": True}},
                LATER, advance_phase=False,
            )
            self.assertEqual("WORKERS_ACKED", journal.load()["phase"])
            journal.record_step(
                "OLD_SUPERSEDED", supersede_key, "BEFORE", {"operation_key": supersede_key},
                None, NOW,
            )
            journal.record_step(
                "OLD_SUPERSEDED", supersede_key, "APPLIED", None,
                {"operation_key": supersede_key, "readback": {"authority_state": "SUPERSEDED"}},
                LATER,
            )
            self.assertEqual("OLD_SUPERSEDED", journal.load()["phase"])

    def test_activation_crash_boundaries_use_durable_role_state_readback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration("PREPARED"))
            roles_path = root / "roles.json"

            before_state = role_state(1)
            before_state["migration_id"] = "migration-1"
            before_state["migration_phase"] = "PREPARED"
            core.write_role_state(roles_path, before_state)

            # Crash immediately after BEFORE: intent is durable, activation is not.
            journal.record_step(
                "STATE_ACTIVATED", "activate-role-state", "BEFORE",
                {"leader_generation": 1}, None, NOW,
            )
            self.assertEqual("PREPARED", journal.load()["phase"])
            self.assertEqual("ROLL_BACK", journal.recovery_direction())
            self.assertEqual("ROLL_BACK", journal.recovery_direction(role_state_path=roles_path))

            # Crash after the atomic role-state effect but before APPLIED: the
            # securely reloaded durable state is the point-of-no-return readback.
            activated = role_state(2)
            activated["migration_id"] = "migration-1"
            activated["migration_phase"] = "STATE_ACTIVATED"
            core.write_role_state(roles_path, activated, expected_generation=1)
            self.assertEqual("ROLL_FORWARD", journal.recovery_direction(role_state_path=roles_path))
            self.assertEqual("ROLL_BACK", journal.recovery_direction())

            # Crash after APPLIED: journal completion independently proves forward.
            journal.record_step(
                "STATE_ACTIVATED", "activate-role-state", "APPLIED", None,
                {"leader_generation": 2}, LATER,
            )
            self.assertEqual("STATE_ACTIVATED", journal.load()["phase"])
            self.assertEqual("ROLL_FORWARD", journal.recovery_direction())

    def test_candidate_restart_requires_exit_and_same_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            journal = core.MigrationJournal(Path(raw) / "migration.json")
            journal.write(migration())
            with self.assertRaisesRegex(core.ProtocolError, "PRIOR_SESSION_ACTIVE"):
                journal.replace_candidate_session(
                    "new-session", "candidate-lineage", prior_session_exited=False,
                )
            with self.assertRaisesRegex(core.ProtocolError, "RESUME_LINEAGE_MISMATCH"):
                journal.replace_candidate_session(
                    "new-session", "other-lineage", prior_session_exited=True,
                )
            result = journal.replace_candidate_session(
                "new-session", "candidate-lineage", prior_session_exited=True,
            )
            self.assertEqual("new-session", result["candidate"]["active_session_id"])

    def test_pre_activation_rollback_closes_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration())
            active_ledger = root / "leader.jsonl"
            active_ledger.write_bytes(b"active-ledger-evidence\n")
            candidate_path = root / "candidate.jsonl"
            candidate = core.CandidateLedger(
                candidate_path, journal.load,
                "candidate-binding", "candidate-session", 2,
            )
            messages, context = candidate_messages()
            append_candidate_terminal(candidate, messages[0], context)
            before = active_ledger.read_bytes()
            candidate_bytes = candidate_path.read_bytes()
            final_digest = journal.close_candidate_ledger(
                candidate_path, recovery_decision="ROLL_BACK",
            )
            self.assertEqual(before, active_ledger.read_bytes())
            self.assertEqual(candidate_bytes, candidate_path.read_bytes())
            quarantine = candidate_path.with_name(
                candidate_path.name + "." + final_digest + ".quarantine"
            )
            self.assertEqual(candidate_bytes, quarantine.read_bytes())
            self.assertEqual(core.digest(quarantine.read_bytes()), final_digest)
            self.assertEqual(final_digest, journal.load()["candidate_ledger_digest"])
            for message in messages:
                with self.subTest(message_type=message["message_type"]):
                    with self.assertRaisesRegex(core.ProtocolError, "STALE_CANDIDATE"):
                        candidate.append_envelope(message, "RECEIVED", context, recorded_at=LATER)

    def test_candidate_ledger_close_holds_append_lock_through_durable_journal_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration())
            candidate_path = root / "candidate.jsonl"
            candidate = core.CandidateLedger(
                candidate_path, journal.load,
                "candidate-binding", "candidate-session", 2,
            )
            messages, context = candidate_messages()
            append_candidate_terminal(candidate, messages[0], context)
            final_bytes = candidate_path.read_bytes()

            entered_journal_write = threading.Event()
            release_journal_write = threading.Event()
            close_result: dict[str, object] = {}
            original_write = journal.write

            def blocked_write(value: dict) -> str:
                entered_journal_write.set()
                if not release_journal_write.wait(timeout=5):
                    raise RuntimeError("test timed out releasing journal write")
                return original_write(value)

            def close_ledger() -> None:
                try:
                    close_result["digest"] = journal.close_candidate_ledger(
                        candidate_path, recovery_decision="ROLL_BACK",
                    )
                except BaseException as exc:  # captured for assertion in test thread
                    close_result["error"] = exc

            with mock.patch.object(journal, "write", side_effect=blocked_write):
                closer = threading.Thread(target=close_ledger, name="candidate-ledger-closer")
                closer.start()
                self.assertTrue(entered_journal_write.wait(timeout=5))
                with self.assertRaisesRegex(core.ProtocolError, "LOCKED"):
                    candidate.append_envelope(
                        messages[1], "RECEIVED", context, recorded_at=LATER,
                    )
                self.assertEqual(final_bytes, candidate_path.read_bytes())
                release_journal_write.set()
                closer.join(timeout=5)
                self.assertFalse(closer.is_alive())

            if "error" in close_result:
                raise close_result["error"]
            final_digest = close_result["digest"]
            self.assertEqual(core.digest(final_bytes), final_digest)
            quarantine = candidate_path.with_name(
                candidate_path.name + "." + str(final_digest) + ".quarantine"
            )
            self.assertEqual(final_bytes, quarantine.read_bytes())
            self.assertEqual(final_digest, journal.load()["candidate_ledger_digest"])
            with self.assertRaisesRegex(core.ProtocolError, "STALE_CANDIDATE"):
                candidate.append_envelope(
                    messages[1], "RECEIVED", context, recorded_at=LATER,
                )

    def test_candidate_ledger_close_rejects_nonterminal_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration())
            candidate_path = root / "candidate.jsonl"
            candidate = core.CandidateLedger(
                candidate_path, journal.load,
                "candidate-binding", "candidate-session", 2,
            )
            messages, context = candidate_messages()
            candidate.append_envelope(messages[0], "RECEIVED", context, recorded_at=NOW)

            with self.assertRaisesRegex(core.ProtocolError, "CANDIDATE_LEDGER_NONTERMINAL"):
                journal.close_candidate_ledger(
                    candidate_path, recovery_decision="ROLL_BACK",
                )
            self.assertFalse(journal.load()["candidate_ledger_closed"])
            self.assertEqual([], list(root.glob("candidate.jsonl.*.quarantine")))

            candidate.append_envelope(messages[0], "EXECUTING", context, recorded_at=LATER)
            candidate.append_envelope(messages[0], "SUCCEEDED", context, recorded_at=LATER)
            journal.close_candidate_ledger(candidate_path, recovery_decision="ROLL_BACK")
            with self.assertRaisesRegex(core.ProtocolError, "CANDIDATE_LEDGER_CLOSED"):
                journal.close_candidate_ledger(candidate_path, recovery_decision="ROLL_BACK")

    def test_candidate_ledger_fences_active_leader_wrong_binding_and_old_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = core.MigrationJournal(root / "migration.json")
            journal.write(migration())
            messages, context = candidate_messages()
            candidate_path = root / "candidate.jsonl"
            candidate = core.CandidateLedger(
                candidate_path, journal.load,
                "candidate-binding", "candidate-session", 2,
            )
            candidate.append_envelope(messages[0], "RECEIVED", context, recorded_at=NOW)

            active_state = role_state()
            active = core.Ledger(
                root / "leader.jsonl", lambda: active_state, "leader",
                "candidate-binding", "candidate-session", 2,
            )
            with self.assertRaisesRegex(core.ProtocolError, "STALE_WRITER"):
                active.append(("x", "run-1", 2, "m"), SHA_A, "RECEIVED", recorded_at=NOW)

            old_leader = core.CandidateLedger(
                candidate_path, journal.load, "old-binding", "old-session", 2,
            )
            with self.assertRaisesRegex(core.ProtocolError, "STALE_CANDIDATE"):
                old_leader.append_envelope(messages[0], "EXECUTING", context, recorded_at=LATER)

            journal.replace_candidate_session(
                "replacement-session", "candidate-lineage", prior_session_exited=True,
            )
            with self.assertRaisesRegex(core.ProtocolError, "STALE_CANDIDATE"):
                candidate.append_envelope(messages[0], "EXECUTING", context, recorded_at=LATER)
            replacement = core.CandidateLedger(
                candidate_path, journal.load,
                "candidate-binding", "replacement-session", 2,
            )
            resumed = replacement.append_envelope(
                messages[0], "EXECUTING", context, recorded_at=LATER,
            )
            self.assertEqual("replacement-session", resumed["writer_session_id"])


class CompatibilityAndAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact = core.load_json(
            ROOT / "plugins" / "agent-team-workflow" / "compatibility.json"
        )
        cls.row = cls.artifact["rows"][0]

    def observed(self) -> dict:
        row = self.row
        return {
            key: row[key]
            for key in (
                "os", "architecture", "runtime_kind", "shell", "claude_version",
                "codex_version", "plugin_version", "role_state_schema",
                "migration_schema", "envelope_schema",
            )
        } | {
            "install_source": row["install_sources"][0],
            "cache_refreshed": True,
            "fresh_sessions": True,
            "static": copy.deepcopy(row["required_static"]),
            "capabilities": list(row["required_capabilities"]),
            "predicates": list(row["required_predicates"]),
        }

    def probe_context(self) -> dict:
        return {
            "run_id": "run-1", "migration_id": "migration-1",
            "source_generation": 1, "target_generation": 2,
            "coordinator_binding_id": "old-binding",
            "coordinator_session_id": "old-session",
            "candidate_binding_id": "candidate-binding",
            "candidate_session_id": "candidate-session",
            "evidence_started_at": "2026-07-26T11:59:00Z",
            "evidence_expires_at": "2026-07-26T12:30:00Z",
        }

    def probe_records(self, *phases: str) -> list[dict]:
        pairs = {
            "PRE_LAUNCH": (
                ("launchRole", "launch_creates_one_tab_in_existing_pane"),
                ("observeSession", "session_identity_is_stable"),
            ),
            "POST_LAUNCH": (
                ("bindRole", "binding_has_exact_candidate_metadata"),
                ("deliverPrompt", "prompt_output_has_probe_id_nonce_hash"),
                ("reportToLeader", "report_observation_has_operation_key_probe_id_hash"),
                ("observeSession", "session_identity_is_stable"),
            ),
            "PROTECTED_EFFECT": (
                ("reconcile", "binding_parent_matches_generation"),
                ("stopSession", "session_is_stopped"),
            ),
        }
        context = self.probe_context()
        records = []
        for gate_phase in phases:
            for primitive, predicate in pairs[gate_phase]:
                probe_id = f"{gate_phase.lower()}-{primitive}"
                value = {"probe_id": probe_id, "matched": True}
                pre_launch_coordinator = gate_phase == "PRE_LAUNCH"
                if pre_launch_coordinator:
                    target_kind, target_id = "coordinator-session", context["coordinator_session_id"]
                    target_binding = context["coordinator_binding_id"]
                    target_session = context["coordinator_session_id"]
                else:
                    target_kind = "candidate-session" if primitive in ("deliverPrompt", "observeSession") else "candidate-binding"
                    target_id = context["candidate_session_id"] if target_kind == "candidate-session" else context["candidate_binding_id"]
                    target_binding = context["candidate_binding_id"]
                    target_session = context["candidate_session_id"]
                record = {
                    "sequence": len(records) + 1,
                    "gate_phase": gate_phase,
                    "owner": "coordinator",
                    "primitive": primitive,
                    "target": {"kind": target_kind, "id": target_id},
                    "expected": value,
                    "actual": copy.deepcopy(value),
                    "cleanup": {"required": False, "completed": True, "evidence_sha256": None},
                    "probe_id": probe_id,
                    "operation_key": core.operation_key("migration-1", probe_id, primitive),
                    "identities": {
                        "run_id": "run-1", "migration_id": "migration-1",
                        "source_generation": 1, "target_generation": 2,
                        "owner_binding_id": "old-binding", "owner_session_id": "old-session",
                        "target_binding_id": target_binding, "target_session_id": target_session,
                    },
                    "hashes": {
                        "expected_sha256": core.digest(value), "actual_sha256": core.digest(value),
                        "readback_sha256": core.digest(value),
                    },
                    "readback": {"predicate": predicate, "outcome": "COMPLETE", "value": copy.deepcopy(value)},
                    "compatibility_row_id": self.row["id"],
                    "compatibility_digest": core.digest(self.artifact),
                    "outcome": "SUCCEEDED", "recorded_at": NOW,
                }
                records.append(record)
        return records

    def test_pre_launch_targets_are_nonempty_and_identity_correlated(self) -> None:
        launch, observe = self.probe_records("PRE_LAUNCH")
        self.assertEqual(
            {"kind": "coordinator-session", "id": "old-session"},
            launch["target"],
        )
        self.assertEqual("old-binding", launch["identities"]["target_binding_id"])
        self.assertEqual("old-session", launch["identities"]["target_session_id"])
        self.assertEqual(
            {"kind": "coordinator-session", "id": "old-session"},
            observe["target"],
        )
        self.assertEqual("old-binding", observe["identities"]["target_binding_id"])
        self.assertEqual("old-session", observe["identities"]["target_session_id"])
        self.assertTrue(all(record["target"]["id"] for record in (launch, observe)))

        context = self.probe_context()
        missing_role_identity = copy.deepcopy([launch, observe])
        missing_role_identity[1]["identities"]["target_binding_id"] = None
        missing_role_identity[1]["identities"]["target_session_id"] = None
        with self.assertRaisesRegex(core.ProtocolError, "GATE_CORRELATION_MISMATCH"):
            core.validate_gate_probe_records(
                missing_role_identity, context,
                compatibility_row_id=self.row["id"],
                compatibility_artifact_digest=core.digest(self.artifact),
            )

        layout_target = copy.deepcopy([launch, observe])
        layout_target[0]["target"] = {"kind": "layout", "id": "layout-anywhere"}
        with self.assertRaisesRegex(core.ProtocolError, "GATE_CORRELATION_MISMATCH"):
            core.validate_gate_probe_records(
                layout_target, context,
                compatibility_row_id=self.row["id"],
                compatibility_artifact_digest=core.digest(self.artifact),
            )

    def with_probe_evidence(self, *phases: str) -> dict:
        observed = self.observed()
        observed["probe_context"] = self.probe_context()
        observed["probe_records"] = self.probe_records(*phases)
        return observed

    def test_supported_tuple_obeys_pre_post_protected_gate_order(self) -> None:
        observed = self.with_probe_evidence("PRE_LAUNCH")
        pre = core.gate_compatibility(self.artifact, observed, phase="PRE_LAUNCH")
        self.assertEqual(self.row["id"], pre["row_id"])
        with self.assertRaisesRegex(core.ProtocolError, "MISSING_GATE_EVIDENCE"):
            core.gate_compatibility(self.artifact, observed, phase="POST_LAUNCH")
        observed = self.with_probe_evidence("PRE_LAUNCH", "POST_LAUNCH")
        core.gate_compatibility(self.artifact, observed, phase="POST_LAUNCH")
        core.gate_compatibility(self.artifact, observed, phase="PROTECTED_MUTATION")
        with self.assertRaisesRegex(core.ProtocolError, "MISSING_GATE_EVIDENCE"):
            core.gate_compatibility(self.artifact, observed, phase="RELEASE")
        release = self.with_probe_evidence("PRE_LAUNCH", "POST_LAUNCH", "PROTECTED_EFFECT")
        core.gate_compatibility(self.artifact, release, phase="RELEASE")

    def test_gate_records_reject_missing_mismatch_stale_reordered_and_indeterminate(self) -> None:
        valid = self.with_probe_evidence("PRE_LAUNCH", "POST_LAUNCH")
        cases: list[tuple[str, dict]] = []

        missing = copy.deepcopy(valid)
        missing["probe_records"] = [
            item for item in missing["probe_records"]
            if item["readback"]["predicate"] != "prompt_output_has_probe_id_nonce_hash"
        ]
        for index, record in enumerate(missing["probe_records"], 1):
            record["sequence"] = index
        cases.append(("MISSING_GATE_EVIDENCE", missing))

        mismatched = copy.deepcopy(valid)
        mismatched["probe_records"][0]["operation_key"] = "fabricated-operation-key"
        cases.append(("GATE_CORRELATION_MISMATCH", mismatched))

        stale = copy.deepcopy(valid)
        stale["probe_records"][0]["recorded_at"] = "2026-07-26T11:58:59Z"
        cases.append(("STALE_GATE_EVIDENCE", stale))

        reordered = copy.deepcopy(valid)
        reordered["probe_records"][0], reordered["probe_records"][1] = reordered["probe_records"][1], reordered["probe_records"][0]
        cases.append(("REORDERED_GATE_EVIDENCE", reordered))

        indeterminate = copy.deepcopy(valid)
        indeterminate["probe_records"][2]["readback"]["outcome"] = "UNKNOWN"
        cases.append(("GATE_READBACK_MISMATCH", indeterminate))

        wrong_identity = copy.deepcopy(valid)
        wrong_identity["probe_records"][2]["identities"]["owner_session_id"] = "stale-session"
        cases.append(("GATE_CORRELATION_MISMATCH", wrong_identity))

        wrong_hash = copy.deepcopy(valid)
        wrong_hash["probe_records"][2]["hashes"]["actual_sha256"] = SHA_B
        cases.append(("GATE_CORRELATION_MISMATCH", wrong_hash))

        for code, observed in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(core.ProtocolError, code):
                    core.gate_compatibility(self.artifact, observed, phase="PROTECTED_MUTATION")

    def test_fabricated_gate_booleans_never_authorize_mutation(self) -> None:
        observed = self.with_probe_evidence("PRE_LAUNCH", "POST_LAUNCH")
        observed["pre_launch_passed"] = True
        observed["post_launch_passed"] = True
        with self.assertRaisesRegex(core.ProtocolError, "LEGACY_GATE_ASSERTION"):
            core.gate_compatibility(self.artifact, observed, phase="PROTECTED_MUTATION")

        no_records = self.observed()
        no_records["pre_launch_passed"] = True
        no_records["post_launch_passed"] = True
        with self.assertRaisesRegex(core.ProtocolError, "LEGACY_GATE_ASSERTION"):
            core.gate_compatibility(self.artifact, no_records, phase="PROTECTED_MUTATION")

    def test_unlisted_skew_missing_capability_and_static_fail_closed(self) -> None:
        cases = []
        skew = self.observed()
        skew["claude_version"] = "2.1.221"
        cases.append(skew)
        missing = self.observed()
        missing["capabilities"].remove("reportToLeader")
        cases.append(missing)
        static = self.observed()
        static["static"]["binding_schema_present"] = False
        cases.append(static)
        for observed in cases:
            with self.subTest(observed=observed):
                with self.assertRaises(core.ProtocolError):
                    core.gate_compatibility(self.artifact, observed, phase="PRE_LAUNCH")

    def test_tested_only_and_unsupported_rows_never_open_production_gates(self) -> None:
        for status in ("TESTED_ONLY", "UNSUPPORTED"):
            artifact = copy.deepcopy(self.artifact)
            row = copy.deepcopy(self.row)
            row["id"] = "row-" + status.lower()
            row["status"] = status
            row["os"] = status.lower()
            artifact["rows"].append(row)
            observed = self.observed()
            observed["os"] = row["os"]
            with self.subTest(status=status):
                with self.assertRaisesRegex(core.ProtocolError, "UNSUPPORTED_TUPLE"):
                    core.gate_compatibility(artifact, observed, phase="PRE_LAUNCH")

    def test_compatibility_rows_require_every_dimension(self) -> None:
        invalid = copy.deepcopy(self.artifact)
        del invalid["rows"][0]["rollback_path"]
        with self.assertRaisesRegex(core.ProtocolError, "MISSING_FIELD"):
            core.validate_compatibility_artifact(invalid)

    def test_adapter_errors_and_capabilities_are_normalized(self) -> None:
        self.assertEqual(
            "TIMEOUT",
            core.normalize_adapter_error("deliverPrompt", TimeoutError("late"))["code"],
        )
        self.assertEqual(
            "PERMISSION_DENIED",
            core.normalize_adapter_error("bindRole", PermissionError("blocked"))["code"],
        )
        self.assertEqual(
            "INTERNAL",
            core.normalize_adapter_error("reconcile", {"code": "mystery"})["code"],
        )
        self.assertEqual((False, ["b"]), core.validate_adapter_capabilities(["a"], ["b", "a"]))
        with self.assertRaisesRegex(core.ProtocolError, "UNKNOWN_ADAPTER_OPERATION"):
            core.normalize_adapter_error("invented", RuntimeError("no"))

        self.assertEqual(
            (core.EffectPolicy.PURE, None), core.effect_policy_for("parse_and_hash"),
        )
        self.assertEqual(
            core.EffectPolicy.RECONCILABLE_IDEMPOTENT,
            core.effect_policy_for("deliverPrompt")[0],
        )
        self.assertEqual(
            (core.EffectPolicy.UNRECONCILABLE, None),
            core.effect_policy_for("arbitrary_external_effect"),
        )


class ReadinessAndDispatchTests(unittest.TestCase):
    def test_exact_readiness_accepts_only_verified_project_and_bootstrap(self) -> None:
        ready = core.validate_readiness(
            ROOT, ROOT, ROOT,
            skill_loaded=True,
            assignment_received=True,
            role_identity_stable=True,
        )
        self.assertTrue(ready["ready"])
        self.assertEqual(str(ROOT.resolve()), ready["project_root"])

        cases = (
            ("WRONG_CWD", ROOT.parent, ROOT, True, True, True),
            ("WRONG_GIT_ROOT", ROOT, ROOT.parent, True, True, True),
            ("SKILL_NOT_LOADED", ROOT, ROOT, False, True, True),
            ("ASSIGNMENT_NOT_RECEIVED", ROOT, ROOT, True, False, True),
            ("UNSTABLE_ROLE_IDENTITY", ROOT, ROOT, True, True, False),
        )
        for code, cwd, git_root, skill, assignment, stable in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(core.ProtocolError, code):
                    core.validate_readiness(
                        ROOT, cwd, git_root,
                        skill_loaded=skill,
                        assignment_received=assignment,
                        role_identity_stable=stable,
                    )

    def test_target_role_cli_selection_is_heterogeneous_and_closed(self) -> None:
        state = role_state()
        state["roles"][0]["cli_tool"] = "claude"
        self.assertEqual(
            ["claude", "codex", "codex"],
            [core.select_role_cli(state, role) for role in core.ROLES],
        )
        with self.assertRaisesRegex(core.ProtocolError, "INVALID_ROLE"):
            core.select_role_cli(state, "unknown")

    def test_report_and_assignment_work_are_frozen_during_cutover(self) -> None:
        state = role_state()
        state["migration_id"] = "migration-1"
        state["migration_phase"] = "PREPARED"
        for role in core.ROLES:
            with self.subTest(role=role):
                with self.assertRaisesRegex(core.ProtocolError, "ROLE_WORK_FROZEN"):
                    core.authorize_role_work(state, role, 1)
        with self.assertRaisesRegex(core.ProtocolError, "STALE_GENERATION"):
            core.authorize_role_work(state, "executor", 0)

        state["migration_phase"] = "COMMITTED"
        core.authorize_role_work(state, "executor", 1)


if __name__ == "__main__":
    unittest.main()

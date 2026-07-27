#!/usr/bin/env python3
"""Deterministic fake transport and E2E evidence writer for protocol tests."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from protocol_core import (
    ADAPTER_OPERATIONS, MIGRATION_PHASES, CandidateLedger, EffectPolicy,
    MigrationJournal, Observation, ProtocolError, atomic_write, canonical_bytes,
    digest, envelope_request_hash, gate_compatibility, normalize_timestamp,
    operation_key, recover_executing, validate_candidate_sequence,
    validate_applied_effect_correlation,
    validate_compatibility_artifact, validate_cross_cli_handshake_reply,
    validate_envelope, validate_ledger_records, validate_migration_journal,
    validate_role_state,
)


class InjectedCrash(RuntimeError):
    pass


class FakeTransport:
    """In-memory adapter with keyed desired-state effects and crash injection.

    `interrupt_at` names a hook (`before:<operation>` or `after:<operation>`).
    State is mutated before the after-hook, allowing restart/reconciliation tests
    to distinguish absent and complete crash windows.
    """

    def __init__(self, *, interrupt_at: str | None = None):
        self.interrupt_at = interrupt_at
        self.projects: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.prompts: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.effects: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.oracle: dict[str, Any] | None = None
        self.gate_artifact: dict[str, Any] | None = None
        self.gate_observed: dict[str, Any] | None = None
        self.role_state: dict[str, Any] | None = None
        self.role_state_readback: dict[str, Any] | None = None
        self.activation_deliveries: dict[str, dict[str, Any]] = {}
        self.worker_role_state_reads: dict[str, dict[str, Any]] = {}
        self.worker_acks: dict[str, dict[str, Any]] = {}
        self.final_reconciled = False
        self.oracle_trace: list[dict[str, Any]] = []

    def _hook(self, point: str) -> None:
        if self.interrupt_at == point:
            self.interrupt_at = None
            raise InjectedCrash(point)

    @staticmethod
    def crash_points() -> tuple[str, ...]:
        """Enumerate every adapter effect and migration-journal crash boundary."""
        adapter = tuple(f"{side}:{operation}" for operation in ADAPTER_OPERATIONS for side in ("before", "after"))
        journal = tuple(
            f"{point}:journal:{phase}"
            for phase in MIGRATION_PHASES
            for point in ("before", "after-before", "before-applied", "after")
        )
        mutations = tuple(
            f"{side}:mutation:{name}"
            for name in (
                "register_new_leader", "reparent_executor", "reparent_reviewer",
                "activate_state", "final_reconcile", "supersede_old", "stop_old",
            )
            for side in ("before", "after")
        )
        return adapter + journal + mutations

    def checkpoint(self, category: str, name: str, point: str) -> None:
        if point not in ("before", "after-before", "before-applied", "after"):
            raise ProtocolError("INVALID_CHECKPOINT", point)
        self._hook(f"{point}:{category}:{name}")
        self.assert_invariants()

    def configure_oracle(self, *, pane_ids: list[str], authority_generation: int, leader_binding_ids: list[str], worker_binding_ids: list[str], recoverable_parent_ids: list[str]) -> None:
        self.oracle = {
            "pane_ids": set(pane_ids),
            "authority_generation": authority_generation,
            "leader_binding_ids": set(leader_binding_ids),
            "worker_binding_ids": set(worker_binding_ids),
            "recoverable_parent_ids": set(recoverable_parent_ids),
        }
        self.assert_invariants()

    def configure_migration_oracle(
        self, *, pane_ids: list[str], source_generation: int, target_generation: int,
        old_leader_binding_id: str, new_leader_binding_id: str,
        old_leader_session_id: str, worker_binding_ids: list[str],
        candidate_binding_ids: list[str], recoverable_parent_ids: list[str],
        role_state: Mapping[str, Any],
    ) -> None:
        """Install a transition-aware oracle for the supported fake migration."""
        self.oracle = {
            "pane_ids": set(pane_ids),
            "authority_generation": source_generation,
            "source_generation": source_generation,
            "target_generation": target_generation,
            "leader_binding_ids": {old_leader_binding_id, new_leader_binding_id},
            "old_leader_binding_id": old_leader_binding_id,
            "new_leader_binding_id": new_leader_binding_id,
            "old_leader_session_id": old_leader_session_id,
            "worker_binding_ids": set(worker_binding_ids),
            "candidate_binding_ids": set(candidate_binding_ids),
            "recoverable_parent_ids": set(recoverable_parent_ids),
        }
        self.role_state = copy.deepcopy(dict(role_state))
        self.role_state_readback = copy.deepcopy(dict(role_state))
        self.assert_invariants()

    def configure_gate_evidence(
        self, artifact: Mapping[str, Any], observed: Mapping[str, Any],
    ) -> None:
        """Retain normalized evidence, never a caller-authored pass assertion."""
        validated_artifact = validate_compatibility_artifact(artifact)
        captured = copy.deepcopy(dict(observed))
        gate_compatibility(validated_artifact, captured, phase="PRE_LAUNCH")
        gate_compatibility(validated_artifact, captured, phase="POST_LAUNCH")
        self.gate_artifact = validated_artifact
        self.gate_observed = captured

    def require_protected_gate(self) -> None:
        if self.gate_artifact is None or self.gate_observed is None:
            raise ProtocolError("PROTECTED_OPERATION_FORBIDDEN", "both compatibility gates must pass")
        try:
            gate_compatibility(
                self.gate_artifact, self.gate_observed,
                phase="PROTECTED_MUTATION",
            )
        except ProtocolError as exc:
            raise ProtocolError("PROTECTED_OPERATION_FORBIDDEN", str(exc)) from exc

    def assert_invariants(self) -> None:
        if self.oracle is None:
            return
        running_panes = {session.get("pane_id") for session in self.sessions.values() if session.get("running", True)}
        if running_panes != self.oracle["pane_ids"] or len(running_panes) != 3:
            raise ProtocolError("PANE_INVARIANT", f"expected exactly {sorted(self.oracle['pane_ids'])}, got {sorted(running_panes)}")
        active = [
            binding for binding_id, binding in self.bindings.items()
            if binding_id in self.oracle["leader_binding_ids"]
            and binding.get("authority_state") == "ACTIVE"
        ]
        if len(active) != 1:
            raise ProtocolError("AUTHORITY_INVARIANT", f"active leaders={len(active)}")
        if "source_generation" in self.oracle:
            if self.role_state is None or self.role_state_readback is None:
                raise ProtocolError("MISSING_ROLE_STATE_READBACK", "durable authority state")
            if self.role_state != self.role_state_readback:
                raise ProtocolError("ROLE_STATE_READBACK_MISMATCH", "durable authority state")
            state_generation = self.role_state["leader_generation"]
            state_phase = self.role_state["migration_phase"]
            active_id = active[0]["binding_id"]
            if state_generation == self.oracle["source_generation"]:
                if active_id != self.oracle["old_leader_binding_id"]:
                    raise ProtocolError("EARLY_ACTIVE", active_id)
                if any(
                    item.get("authority_state") == "ACTIVE"
                    for item in self.bindings.values()
                    if item.get("binding_id") == self.oracle["new_leader_binding_id"]
                ):
                    raise ProtocolError("DUAL_ACTIVE", self.oracle["new_leader_binding_id"])
            elif state_generation == self.oracle["target_generation"]:
                if active_id != self.oracle["new_leader_binding_id"]:
                    raise ProtocolError("ACTIVATION_READBACK_MISMATCH", active_id)
                if state_phase not in ("STATE_ACTIVATED", "COMMITTED"):
                    raise ProtocolError("EARLY_ACTIVE", state_phase)
                if self.role_state_readback["leader_generation"] != self.oracle["target_generation"]:
                    raise ProtocolError("MISSING_ROLE_STATE_READBACK", "target generation")
                for binding_id in self.oracle["worker_binding_ids"]:
                    worker = self.bindings.get(binding_id)
                    if worker is None or worker.get("leader_generation") != self.oracle["target_generation"] or worker.get("parent_binding_id") != self.oracle["new_leader_binding_id"]:
                        raise ProtocolError("PARENT_GRAPH_INVARIANT", binding_id)
            else:
                raise ProtocolError("AUTHORITY_INVARIANT", str(state_generation))
            old = self.bindings.get(self.oracle["old_leader_binding_id"])
            if self.final_reconciled and set(self.worker_acks) != {"executor", "reviewer"}:
                raise ProtocolError("EARLY_FINAL_RECONCILE", str(sorted(self.worker_acks)))
            if old and old.get("authority_state") == "SUPERSEDED" and not (set(self.worker_acks) == {"executor", "reviewer"} and self.final_reconciled):
                raise ProtocolError("EARLY_OLD_SUPERSEDE", old["binding_id"])
            old_session = self.sessions.get(self.oracle["old_leader_session_id"])
            if old_session and not old_session.get("running", True) and not (set(self.worker_acks) == {"executor", "reviewer"} and self.final_reconciled):
                raise ProtocolError("EARLY_OLD_STOP", self.oracle["old_leader_session_id"])
            if state_generation == self.oracle["target_generation"]:
                for role in ("executor", "reviewer"):
                    ack = self.worker_acks.get(role)
                    if ack is not None:
                        binding = self.bindings.get(ack.get("binding_id"))
                        if binding is None or binding.get("leader_generation") != self.oracle["target_generation"] or binding.get("parent_binding_id") != self.oracle["new_leader_binding_id"]:
                            raise ProtocolError("WORKER_ACK_INVARIANT", role)
                        if role not in self.activation_deliveries or role not in self.worker_role_state_reads:
                            raise ProtocolError("WORKER_ACK_INVARIANT", role)
                        if ack.get("activation_message_id") != self.activation_deliveries[role].get("message_id") or ack.get("roles_sha256") != digest(self.role_state["roles"]):
                            raise ProtocolError("WORKER_ACK_INVARIANT", role)
            self.oracle_trace.append({
                "phase": state_phase, "leader_generation": state_generation,
                "active_leader": active_id, "worker_acks": sorted(self.worker_acks),
                "final_reconciled": self.final_reconciled,
            })
        for binding_id in self.oracle["worker_binding_ids"]:
            binding = self.bindings.get(binding_id)
            if binding is None or binding.get("parent_binding_id") not in self.oracle["recoverable_parent_ids"]:
                raise ProtocolError("PARENT_GRAPH_INVARIANT", binding_id)

    def _effect(
        self, operation: str, key: str, desired: Mapping[str, Any],
        apply: Callable[[], None], *, readback: Callable[[], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._hook("before:" + operation)
        desired_digest = digest(desired)
        previous = self.effects.get(key)
        if previous and previous["desired_digest"] != desired_digest:
            raise ProtocolError("IDEMPOTENCY_CONFLICT", key)
        if not previous:
            apply()
            self.effects[key] = {"operation": operation, "desired_digest": desired_digest}
            sequence = len(self.calls) + 1
            request = {"operation_key": key, "desired": copy.deepcopy(desired)}
            # The recorded response is the adapter's durable read-back value,
            # not an internal idempotency bookkeeping tuple.  This lets gate
            # evidence bind expected/actual/readback to the exact bytes that
            # were returned by the keyed operation.
            response = copy.deepcopy(dict(readback())) if readback else copy.deepcopy(desired)
            self.calls.append({
                "sequence": sequence, "operation": operation, "key": key,
                "desired_digest": desired_digest, "request_id": f"fake-request-{sequence}",
                "response_id": f"fake-response-{sequence}", "request": request,
                "response": response, "request_digest": digest(request),
                "response_digest": digest(response),
            })
        self.assert_invariants()
        self._hook("after:" + operation)
        return copy.deepcopy(self.effects[key])

    def _observation(
        self, operation: str, key: str | None, request: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture keyed request/response provenance for read-only adapter calls."""
        if key is not None:
            sequence = len(self.calls) + 1
            request_value = {"operation_key": key, **copy.deepcopy(dict(request))}
            response_value = copy.deepcopy(dict(response))
            self.calls.append({
                "sequence": sequence, "operation": operation, "key": key,
                "desired_digest": digest(request_value),
                "request_id": f"fake-request-{sequence}",
                "response_id": f"fake-response-{sequence}",
                "request": request_value, "response": response_value,
                "request_digest": digest(request_value),
                "response_digest": digest(response_value),
            })
        return copy.deepcopy(dict(response))

    def resolveProject(self, project_root: str, *, key: str | None = None) -> dict[str, Any]:
        self._hook("before:resolveProject")
        value = self.projects.get(project_root)
        if value is None:
            raise ProtocolError("NOT_FOUND", project_root)
        self._hook("after:resolveProject")
        return self._observation(
            "resolveProject", key, {"project_root": project_root}, value,
        )

    def launchRole(self, key: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(descriptor["session_id"])
        self._effect("launchRole", key, descriptor, lambda: self.sessions.__setitem__(session_id, dict(descriptor, running=True)))
        return copy.deepcopy(self.sessions[session_id])

    def resumeRole(self, key: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(descriptor["session_id"])
        self._effect("resumeRole", key, descriptor, lambda: self.sessions.__setitem__(session_id, dict(descriptor, running=True)))
        return copy.deepcopy(self.sessions[session_id])

    def deliverPrompt(self, key: str, session_id: str, probe_id: str, nonce: str, request_hash: str, *, message_id: str | None = None) -> dict[str, Any]:
        desired = {"session_id": session_id, "probe_id": probe_id, "nonce": nonce, "request_hash": request_hash}
        if message_id is not None:
            desired["message_id"] = message_id
        def apply() -> None:
            self.prompts[key] = desired
            if probe_id.startswith("activation-"):
                self.activation_deliveries[probe_id.removeprefix("activation-")] = {
                    "message_id": message_id, "request_hash": request_hash,
                }
        self._effect("deliverPrompt", key, desired, apply)
        return copy.deepcopy(self.prompts[key])

    def observeSession(self, session_id: str, *, key: str | None = None) -> dict[str, Any]:
        self._hook("before:observeSession")
        if session_id not in self.sessions:
            raise ProtocolError("NOT_FOUND", session_id)
        self._hook("after:observeSession")
        return self._observation(
            "observeSession", key, {"session_id": session_id},
            self.sessions[session_id],
        )

    def stopSession(self, key: str, session_id: str) -> dict[str, Any]:
        desired = {"session_id": session_id, "running": False}
        def apply() -> None:
            if session_id not in self.sessions:
                raise ProtocolError("NOT_FOUND", session_id)
            self.sessions[session_id]["running"] = False
        self._effect("stopSession", key, desired, apply)
        return {"session_id": session_id, "running": bool(self.sessions[session_id]["running"])}

    def activateRoleState(self, key: str, role_state: Mapping[str, Any]) -> dict[str, Any]:
        desired = {"role_state": copy.deepcopy(dict(role_state))}
        def apply() -> None:
            state = copy.deepcopy(dict(role_state))
            self.role_state = state
            self.role_state_readback = copy.deepcopy(state)
            new_binding_id = next(role["binding_id"] for role in state["roles"] if role["role"] == "leader")
            old_binding_id = self.oracle["old_leader_binding_id"] if self.oracle else None
            if old_binding_id and old_binding_id in self.bindings:
                self.bindings[old_binding_id]["authority_state"] = "FROZEN"
            if new_binding_id in self.bindings:
                self.bindings[new_binding_id]["authority_state"] = "ACTIVE"
        self._effect("activateRoleState", key, desired, apply)
        return copy.deepcopy(self.role_state or {})

    def observeRoleState(self, key: str, role: str) -> dict[str, Any]:
        self._hook("before:observeRoleState")
        if self.role_state is None or self.role_state_readback is None:
            raise ProtocolError("MISSING_ROLE_STATE_READBACK", role)
        self.worker_role_state_reads[role] = copy.deepcopy(self.role_state_readback)
        self._hook("after:observeRoleState")
        return self._observation("observeRoleState", key, {"role": role}, self.role_state_readback)

    def bindRole(self, key: str, binding: Mapping[str, Any]) -> dict[str, Any]:
        binding_id = str(binding["binding_id"])
        self._effect("bindRole", key, binding, lambda: self.bindings.__setitem__(binding_id, dict(binding)))
        return copy.deepcopy(self.bindings[binding_id])

    def updateMilestone(self, key: str, binding_id: str, milestone: Mapping[str, Any]) -> dict[str, Any]:
        desired = {"binding_id": binding_id, "milestone": dict(milestone)}
        def apply() -> None:
            if binding_id not in self.bindings:
                raise ProtocolError("NOT_FOUND", binding_id)
            self.bindings[binding_id]["milestone"] = dict(milestone)
        self._effect("updateMilestone", key, desired, apply)
        return copy.deepcopy(self.bindings[binding_id])

    def reportToLeader(self, key: str, report: Mapping[str, Any]) -> dict[str, Any]:
        def apply() -> None:
            self.reports[key] = dict(report)
            if report.get("kind") == "ACTIVATION_ACK":
                self.worker_acks[str(report["role"])] = dict(report)
        self._effect("reportToLeader", key, report, apply)
        return copy.deepcopy(self.reports[key])

    def reconciliation_readback(self) -> dict[str, Any]:
        return {
            "sessions": copy.deepcopy(self.sessions),
            "bindings": copy.deepcopy(self.bindings),
            "effects": copy.deepcopy(self.effects),
            "final_reconciled": self.final_reconciled,
        }

    def reconcile(self, *, key: str | None = None, final: bool = False) -> dict[str, Any]:
        if final:
            if key is None:
                raise ProtocolError("MISSING_OPERATION_KEY", "final reconcile")

            def apply_final() -> None:
                self.final_reconciled = True

            self._effect(
                "reconcile", key, {"final": True}, apply_final,
                readback=self.reconciliation_readback,
            )
            return self.reconciliation_readback()
        self._hook("before:reconcile")
        self.assert_invariants()
        self._hook("after:reconcile")
        value = self.reconciliation_readback()
        return self._observation("reconcile", key, {}, value)

    def observe_effect(self, key: str, desired: Mapping[str, Any]) -> str:
        existing = self.effects.get(key)
        if existing is None:
            return "ABSENT"
        return "COMPLETE" if existing["desired_digest"] == digest(desired) else "UNKNOWN"


class FakeMigrationCoordinator:
    """Journal-before/effect/journal-after coordinator used by exhaustive faults."""

    PROTECTED_MUTATIONS = (
        "register_new_leader", "reparent_executor", "reparent_reviewer",
        "activate_state", "final_reconcile", "supersede_old", "stop_old",
    )

    def __init__(self, transport: FakeTransport, journal: MigrationJournal):
        self.transport = transport
        self.journal = journal

    def write_phase(self, phase: str, mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if phase not in MIGRATION_PHASES:
            raise ProtocolError("INVALID_PHASE", phase)
        self.transport.checkpoint("journal", phase, "before")
        value = self.journal.load()
        operation = "migration:" + phase.lower()
        before_snapshot = {
            "phase": value["phase"],
            "candidate": copy.deepcopy(value["candidate"]),
            "observed_binding_parents": copy.deepcopy(value["observed_binding_parents"]),
        }
        value["steps"].append({
            "sequence": len(value["steps"]) + 1, "phase": phase,
            "operation": operation, "status": "BEFORE", "before": before_snapshot,
            "after": None, "recorded_at": "2026-07-26T12:00:00Z",
        })
        self.journal.write(value)
        self.transport.checkpoint("journal", phase, "after-before")
        if mutate:
            mutate(value)
        self.transport.checkpoint("journal", phase, "before-applied")
        after_snapshot = {
            "phase": phase,
            "candidate": copy.deepcopy(value["candidate"]),
            "observed_binding_parents": copy.deepcopy(value["observed_binding_parents"]),
        }
        value["steps"].append({
            "sequence": len(value["steps"]) + 1, "phase": phase,
            "operation": operation, "status": "APPLIED", "before": None,
            "after": after_snapshot, "recorded_at": "2026-07-26T12:00:00Z",
        })
        value["phase"] = phase
        self.journal.write(value)
        self.transport.checkpoint("journal", phase, "after")
        return value

    def protected_mutation(
        self, name: str, effect: Callable[[], Any], *, reconcile_around: bool = True,
    ) -> Any:
        if name not in self.PROTECTED_MUTATIONS:
            raise ProtocolError("UNKNOWN_PROTECTED_MUTATION", name)
        self.transport.require_protected_gate()
        self.transport.checkpoint("mutation", name, "before")
        if reconcile_around:
            self.transport.reconcile()
        result = effect()
        if reconcile_around:
            self.transport.reconcile()
        self.transport.checkpoint("mutation", name, "after")
        return result

    def journaled_effect(
        self, *, phase: str, key: str, desired: Mapping[str, Any],
        readback_predicate: str, effect: Callable[[], Mapping[str, Any]],
        readback: Callable[[], Mapping[str, Any]],
        validate_readback: Callable[[Mapping[str, Any]], bool],
        timestamp: Callable[[], str], advance_phase: bool = True,
    ) -> dict[str, Any]:
        """Run or recover one exact-key external effect from durable intent.

        A crash before the adapter call leaves a proven-absent effect that is
        safely retried with the same key.  A crash after the adapter call but
        before APPLIED is recovered from the adapter read-back without executing
        again.  Unknown correlation or an invalid/missing read-back is terminally
        ambiguous and therefore fails closed.
        """
        if phase not in MIGRATION_PHASES:
            raise ProtocolError("INVALID_PHASE", phase)
        desired_value = copy.deepcopy(dict(desired))
        desired_sha256 = digest(desired_value)
        journal = self.journal.load()
        matching = [step for step in journal["steps"] if step["operation"] == key]
        statuses = [step["status"] for step in matching]
        if statuses not in ([], ["BEFORE"], ["BEFORE", "APPLIED"]):
            raise ProtocolError("INVALID_EFFECT_JOURNAL", key)
        before_evidence = matching[0]["before"] if matching else None
        if matching and (
            any(step["phase"] != phase for step in matching)
            or not isinstance(before_evidence, Mapping)
            or before_evidence.get("operation_key") != key
            or before_evidence.get("desired_sha256") != desired_sha256
            or before_evidence.get("readback_predicate") != readback_predicate
        ):
            raise ProtocolError("MISKEYED_EFFECT_JOURNAL", key)
        if statuses == ["BEFORE", "APPLIED"]:
            recovered = validate_applied_effect_correlation(
                key, before_evidence, matching[1]["after"],
            )
            if (
                recovered["desired_sha256"] != desired_sha256
                or recovered["readback_predicate"] != readback_predicate
            ):
                raise ProtocolError("MISKEYED_EFFECT_JOURNAL", key)
            value = copy.deepcopy(dict(recovered["readback"]))
            if recovered.get("readback_sha256") != digest(value) or not validate_readback(value):
                raise ProtocolError("INDETERMINATE_FINAL_EFFECT", key)
            return value
        if not matching:
            self.journal.record_step(
                phase, key, "BEFORE",
                {
                    "operation_key": key, "desired_sha256": desired_sha256,
                    "readback_predicate": readback_predicate,
                },
                None, timestamp(), advance_phase=False,
            )

        observation = self.transport.observe_effect(key, desired_value)
        if observation == "UNKNOWN":
            raise ProtocolError("INDETERMINATE_FINAL_EFFECT", key)
        if observation == "ABSENT":
            effect()
        # Always bind APPLIED to an explicit adapter read-back.  The direct call
        # result is not sufficient evidence and may be unavailable after a crash
        # in the post-effect/pre-APPLIED window.
        value = readback()
        if not isinstance(value, Mapping) or not validate_readback(value):
            raise ProtocolError("INDETERMINATE_FINAL_EFFECT", key)
        value = copy.deepcopy(dict(value))
        self.journal.record_step(
            phase, key, "APPLIED", None,
            {
                "operation_key": key, "desired_sha256": desired_sha256,
                "readback_predicate": readback_predicate,
                "readback": value, "readback_sha256": digest(value),
                "outcome": "SUCCEEDED",
            },
            timestamp(), advance_phase=advance_phase,
        )
        return value


E2E_FIELDS = (
    "schema_version", "run_id", "protocol_version", "plugin_version", "schema_versions",
    "compatibility_row_id", "compatibility_digest",
    "before_role_state", "after_role_state", "before_bindings", "after_bindings",
    "identities", "tool_events", "migration_journal", "migration_journal_digest",
    "candidate_ledger_records", "gate_evidence", "envelopes", "acknowledgements",
    "timestamps", "probe_results", "assertions", "command_results", "outcome",
)


def _validate_e2e_trace(
    evidence: Mapping[str, Any], journal: Mapping[str, Any],
    gate_records: list[Mapping[str, Any]],
) -> None:
    """Bind gates and journal transitions to one chronological adapter trace."""
    tool_by_key: dict[str, Mapping[str, Any]] = {}
    prior_sequence = 0
    for event in evidence["tool_events"]:
        if set(event) != {
            "sequence", "operation", "request_id", "response_id", "request",
            "response", "request_digest", "response_digest",
        } or event["sequence"] != prior_sequence + 1:
            raise ProtocolError("INVALID_E2E_TRACE", "exact ordered tool events required")
        if event["request_digest"] != digest(event["request"]) or event["response_digest"] != digest(event["response"]):
            raise ProtocolError("INVALID_E2E_TRACE", "tool request/response digest")
        prior_sequence = event["sequence"]
        operation_key_value = event["request"].get("operation_key")
        if not isinstance(operation_key_value, str) or not operation_key_value or operation_key_value in tool_by_key:
            raise ProtocolError("INVALID_E2E_TRACE", "unique keyed tool request required")
        tool_by_key[operation_key_value] = event

    steps_by_operation: dict[str, list[Mapping[str, Any]]] = {}
    for step in journal["steps"]:
        steps_by_operation.setdefault(step["operation"], []).append(step)

    gate_events: list[Mapping[str, Any]] = []
    pre_keys: list[str] = []
    post_keys: list[str] = []
    for record in gate_records:
        key = record["operation_key"]
        event = tool_by_key.get(key)
        if event is None or event["operation"] != record["primitive"]:
            raise ProtocolError("DISJOINT_GATE_TRACE", key)
        if event["response"] != record["actual"] or record["readback"]["value"] != event["response"]:
            raise ProtocolError("INVALID_E2E_TRACE", "gate response/readback")
        correlated_steps = steps_by_operation.get(key, [])
        if [step["status"] for step in correlated_steps] != ["BEFORE", "APPLIED"]:
            raise ProtocolError("INVALID_E2E_TRACE", "gate journal pair")
        expected_phase = "PREFLIGHTED" if record["gate_phase"] == "PRE_LAUNCH" else "VALIDATED"
        if any(step["phase"] != expected_phase for step in correlated_steps):
            raise ProtocolError("INVALID_E2E_TRACE", "gate journal phase")
        gate_events.append(event)
        (pre_keys if record["gate_phase"] == "PRE_LAUNCH" else post_keys).append(key)
    for operation in steps_by_operation:
        if operation not in tool_by_key:
            raise ProtocolError("SYNTHETIC_MIGRATION_STEP", operation)
    if [event["sequence"] for event in gate_events] != sorted(event["sequence"] for event in gate_events):
        raise ProtocolError("REORDERED_GATE_TRACE", "tool event order")

    candidate_session_id = journal["candidate"]["active_session_id"]
    candidate_launches = [
        event for event in evidence["tool_events"]
        if event["operation"] == "launchRole"
        and event["request"].get("desired", {}).get("session_id") == candidate_session_id
    ]
    if len(candidate_launches) != 1:
        raise ProtocolError("INVALID_E2E_TRACE", "exact candidate launch")
    launch = candidate_launches[0]
    if any(tool_by_key[key]["sequence"] >= launch["sequence"] for key in pre_keys):
        raise ProtocolError("EARLY_CANDIDATE_LAUNCH", candidate_session_id)
    if any(tool_by_key[key]["sequence"] <= launch["sequence"] for key in post_keys):
        raise ProtocolError("REORDERED_GATE_TRACE", "post-launch evidence")
    launch_steps = steps_by_operation.get(launch["request"]["operation_key"], [])
    if [step["status"] for step in launch_steps] != ["BEFORE", "APPLIED"] or any(step["phase"] != "PREPARED" for step in launch_steps):
        raise ProtocolError("INVALID_E2E_TRACE", "candidate launch journal")

    validated_steps = [step for step in journal["steps"] if step["phase"] == "VALIDATED"]
    if (
        [step["operation"] for step in validated_steps if step["status"] == "BEFORE"] != post_keys
        or [step["operation"] for step in validated_steps if step["status"] == "APPLIED"] != post_keys
        or [step["status"] for step in validated_steps] != ["BEFORE"] * len(post_keys) + ["APPLIED"] * len(post_keys)
    ):
        raise ProtocolError("INVALID_E2E_TRACE", "VALIDATED must follow terminal POST_LAUNCH evidence")

    def phase_operations(phase: str) -> list[str]:
        return [
            step["operation"] for step in journal["steps"]
            if step["phase"] == phase and step["status"] == "APPLIED"
        ]

    def one_phase_event(phase: str) -> Mapping[str, Any]:
        operations = phase_operations(phase)
        if len(operations) != 1 or operations[0] not in tool_by_key:
            raise ProtocolError("INVALID_AUTHORITY_TRACE", phase)
        return tool_by_key[operations[0]]

    registered_event = one_phase_event("NEW_LEADER_REGISTERED")
    executor_event = one_phase_event("EXECUTOR_REPARENTED")
    reviewer_event = one_phase_event("REVIEWER_REPARENTED")
    activation_event = one_phase_event("STATE_ACTIVATED")
    workers_acked_event = one_phase_event("WORKERS_ACKED")
    old_operations = phase_operations("OLD_SUPERSEDED")
    committed_operations = phase_operations("COMMITTED")
    if len(old_operations) != 2 or len(committed_operations) != 1:
        raise ProtocolError("INVALID_AUTHORITY_TRACE", "final effect journal cardinality")
    if any(key not in tool_by_key for key in (*old_operations, *committed_operations)):
        raise ProtocolError("INVALID_AUTHORITY_TRACE", "final effect tool correlation")
    final_reconcile_event, supersede_event = (tool_by_key[key] for key in old_operations)
    stop_event = tool_by_key[committed_operations[0]]
    phase_sequences = [
        registered_event["sequence"], executor_event["sequence"],
        reviewer_event["sequence"], activation_event["sequence"],
        workers_acked_event["sequence"], final_reconcile_event["sequence"],
        supersede_event["sequence"], stop_event["sequence"],
    ]
    if phase_sequences != sorted(phase_sequences) or len(set(phase_sequences)) != len(phase_sequences):
        raise ProtocolError("INVALID_AUTHORITY_TRACE", "phase effect order")

    old_binding_id = journal["old_leader"]["binding_id"]
    new_binding_id = journal["candidate"]["binding_id"]
    target_generation = journal["target_generation"]

    def validate_final_effect_pair(
        phase: str, event: Mapping[str, Any], predicate: str,
    ) -> None:
        key = event["request"]["operation_key"]
        steps = steps_by_operation.get(key, [])
        if [step["status"] for step in steps] != ["BEFORE", "APPLIED"] or any(step["phase"] != phase for step in steps):
            raise ProtocolError("INVALID_FINAL_EFFECT_JOURNAL", key)
        before, after = steps[0]["before"], steps[1]["after"]
        desired = event["request"].get("desired")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping) or not isinstance(desired, Mapping):
            raise ProtocolError("INVALID_FINAL_EFFECT_JOURNAL", key)
        if (
            before.get("operation_key") != key
            or before.get("desired_sha256") != digest(desired)
            or before.get("readback_predicate") != predicate
            or after.get("operation_key") != key
            or after.get("desired_sha256") != digest(desired)
            or after.get("readback_predicate") != predicate
            or after.get("readback") != event["response"]
            or after.get("readback_sha256") != digest(event["response"])
            or after.get("outcome") != "SUCCEEDED"
        ):
            raise ProtocolError("INVALID_FINAL_EFFECT_JOURNAL", key)

    expected_final_key = operation_key(journal["migration_id"], "commit-readback", "reconcile")
    expected_supersede_key = operation_key(journal["migration_id"], "supersede-old", "bindRole")
    expected_stop_key = operation_key(journal["migration_id"], "stop-old", "stopSession")
    if [
        final_reconcile_event["request"]["operation_key"],
        supersede_event["request"]["operation_key"],
        stop_event["request"]["operation_key"],
    ] != [expected_final_key, expected_supersede_key, expected_stop_key]:
        raise ProtocolError("MISKEYED_FINAL_EFFECT", "reconcile/supersede/stop")
    validate_final_effect_pair(
        "OLD_SUPERSEDED", final_reconcile_event,
        "complete_generation_graph_with_old_fenced",
    )
    validate_final_effect_pair(
        "OLD_SUPERSEDED", supersede_event,
        "old_binding_exactly_superseded",
    )
    validate_final_effect_pair(
        "COMMITTED", stop_event, "old_session_exactly_stopped",
    )
    registered = registered_event["response"]
    if (
        registered_event["operation"] != "bindRole"
        or registered.get("binding_id") != new_binding_id
        or registered.get("leader_generation") != target_generation
        or registered.get("authority_state") != "CANDIDATE"
    ):
        raise ProtocolError("EARLY_ACTIVE", new_binding_id)
    for event in evidence["tool_events"]:
        response = event["response"]
        if (
            event["sequence"] < activation_event["sequence"]
            and response.get("binding_id") == new_binding_id
            and response.get("authority_state") == "ACTIVE"
        ):
            raise ProtocolError("EARLY_ACTIVE", new_binding_id)

    for event, role in ((executor_event, "executor"), (reviewer_event, "reviewer")):
        response = event["response"]
        if (
            event["operation"] != "bindRole" or response.get("role") != role
            or response.get("leader_generation") != target_generation
            or response.get("parent_binding_id") != new_binding_id
        ):
            raise ProtocolError("INVALID_AUTHORITY_TRACE", role + " reparent")

    if activation_event["operation"] != "activateRoleState":
        raise ProtocolError("MISSING_ROLE_STATE_READBACK", "STATE_ACTIVATED")
    activation_body = activation_event["response"].get("role_state")
    if activation_body is None:
        activation_body = activation_event["request"].get("desired", {}).get("role_state")
    try:
        activated_state = validate_role_state(activation_body)
    except (ProtocolError, TypeError) as exc:
        raise ProtocolError("MISSING_ROLE_STATE_READBACK", "STATE_ACTIVATED") from exc
    if activated_state["leader_generation"] != target_generation or activated_state["migration_phase"] != "STATE_ACTIVATED":
        raise ProtocolError("ACTIVATION_READBACK_MISMATCH", "generation/phase")
    activated_by_role = {role["role"]: role for role in activated_state["roles"]}
    if activated_by_role["leader"]["binding_id"] != new_binding_id or activated_by_role["leader"]["authority_state"] != "ACTIVE":
        raise ProtocolError("ACTIVATION_READBACK_MISMATCH", "leader")
    for role in ("executor", "reviewer"):
        worker = activated_by_role[role]
        if worker["leader_generation"] != target_generation or worker["parent_binding_id"] != new_binding_id:
            raise ProtocolError("ACTIVATION_READBACK_MISMATCH", role)

    envelopes = [item.get("envelope") for item in evidence["envelopes"] if isinstance(item, Mapping)]
    activations = [item for item in envelopes if isinstance(item, Mapping) and item.get("message_type") == "LEADER_ACTIVATED"]
    if len(activations) != 2 or {item["recipient"]["role"] for item in activations} != {"executor", "reviewer"}:
        raise ProtocolError("MISSING_WORKER_ACTIVATION_ACK", "activation envelopes")
    receipt_by_request = {
        item["reply_to"]: item for item in envelopes
        if isinstance(item, Mapping) and item.get("message_type") == "RECEIPT_ACK" and item.get("reply_to")
    }
    for activation in activations:
        role = activation["recipient"]["role"]
        activation_hash = envelope_request_hash(activation)
        deliveries = [
            event for event in evidence["tool_events"]
            if event["operation"] == "deliverPrompt"
            and event["response"].get("message_id") == activation["message_id"]
            and event["response"].get("request_hash") == activation_hash
        ]
        reads = [
            event for event in evidence["tool_events"]
            if event["operation"] == "observeRoleState"
            and event["request"].get("role") == role
        ]
        receipt = receipt_by_request.get(activation["message_id"])
        if len(deliveries) != 1 or len(reads) != 1 or receipt is None:
            raise ProtocolError("MISSING_WORKER_ACTIVATION_ACK", role)
        read_state = validate_role_state(reads[0]["response"])
        read_worker = next(item for item in read_state["roles"] if item["role"] == role)
        if read_state["leader_generation"] != target_generation or read_worker["parent_binding_id"] != new_binding_id:
            raise ProtocolError("WORKER_ACTIVATION_READBACK_MISMATCH", role)
        ack_key = receipt["payload"].get("operation_key")
        ack_event = tool_by_key.get(ack_key)
        if ack_event is None or ack_event["operation"] != "reportToLeader":
            raise ProtocolError("MISSING_WORKER_ACTIVATION_ACK", role)
        ack = ack_event["response"]
        if (
            ack.get("kind") != "ACTIVATION_ACK" or ack.get("role") != role
            or ack.get("leader_generation") != target_generation
            or ack.get("parent_binding_id") != new_binding_id
            or ack.get("activation_message_id") != activation["message_id"]
            or ack.get("activation_sha256") != activation_hash
            or ack.get("roles_sha256") != digest(activated_state["roles"])
            or ack.get("role_state_sha256") != digest(read_state)
            or ack.get("outcome") != "SUCCEEDED"
        ):
            raise ProtocolError("WORKER_ACTIVATION_READBACK_MISMATCH", role)
        if not (
            activation_event["sequence"] < deliveries[0]["sequence"]
            < reads[0]["sequence"] < ack_event["sequence"]
            < workers_acked_event["sequence"]
        ):
            raise ProtocolError("EARLY_WORKER_ACK", role)

    if workers_acked_event["response"].get("workers") != ["executor", "reviewer"]:
        raise ProtocolError("MISSING_WORKER_ACTIVATION_ACK", "aggregate")
    final_graph = final_reconcile_event["response"]
    final_bindings = final_graph.get("bindings", {})
    final_sessions = final_graph.get("sessions", {})
    if (
        final_reconcile_event["operation"] != "reconcile"
        or final_reconcile_event["request"].get("desired") != {"final": True}
        or final_graph.get("final_reconciled") is not True
        or final_bindings.get(old_binding_id, {}).get("authority_state") != "FROZEN"
        or final_bindings.get(new_binding_id, {}).get("authority_state") != "ACTIVE"
        or final_sessions.get(journal["old_leader"]["session_id"], {}).get("running") is not True
    ):
        raise ProtocolError("INVALID_FINAL_RECONCILE_READBACK", expected_final_key)
    for role in ("executor", "reviewer"):
        binding_id = activated_by_role[role]["binding_id"]
        binding_value = final_bindings.get(binding_id, {})
        if binding_value.get("leader_generation") != target_generation or binding_value.get("parent_binding_id") != new_binding_id:
            raise ProtocolError("INVALID_FINAL_RECONCILE_READBACK", role)
    if (
        supersede_event["operation"] != "bindRole"
        or supersede_event["response"].get("binding_id") != old_binding_id
        or supersede_event["response"].get("authority_state") != "SUPERSEDED"
        or stop_event["operation"] != "stopSession"
        or stop_event["response"] != {"session_id": journal["old_leader"]["session_id"], "running": False}
    ):
        raise ProtocolError("INVALID_AUTHORITY_TRACE", "old leader cleanup readback")
    if not (
        workers_acked_event["sequence"] < final_reconcile_event["sequence"]
        < supersede_event["sequence"] < stop_event["sequence"]
    ):
        raise ProtocolError("EARLY_OLD_STOP", journal["old_leader"]["session_id"])
    if [
        final_reconcile_event["sequence"], supersede_event["sequence"], stop_event["sequence"],
    ] != [prior_sequence - 2, prior_sequence - 1, prior_sequence]:
        raise ProtocolError("INVALID_AUTHORITY_TRACE", "final effects must be contiguous")
    if stop_event["sequence"] != evidence["tool_events"][-1]["sequence"]:
        raise ProtocolError("EARLY_OLD_STOP", "stop-old must be final tool effect")


def validate_e2e_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise ProtocolError("INVALID_E2E", "object required")
    missing, extra = set(E2E_FIELDS) - set(evidence), set(evidence) - set(E2E_FIELDS)
    if missing or extra:
        raise ProtocolError("INVALID_E2E_FIELDS", f"missing={sorted(missing)} extra={sorted(extra)}")
    if evidence["schema_version"] != 1 or evidence["protocol_version"] != "0.2.0" or evidence["plugin_version"] != "0.2.0":
        raise ProtocolError("UNSUPPORTED_E2E_VERSION", "expected schema 1 / protocol 0.2.0")
    if evidence["schema_versions"] != {"role_state": 1, "migration": 1, "envelope": 1}:
        raise ProtocolError("INVALID_E2E_SCHEMAS", str(evidence["schema_versions"]))
    if evidence["outcome"] not in ("COMMITTED", "ROLLED_BACK", "ABORTED"):
        raise ProtocolError("INVALID_E2E_OUTCOME", str(evidence["outcome"]))
    if not evidence["compatibility_row_id"] or not isinstance(evidence["compatibility_digest"], str) or len(evidence["compatibility_digest"]) != 64:
        raise ProtocolError("INVALID_COMPATIBILITY_PROVENANCE", "row/digest")
    before_state = validate_role_state(evidence["before_role_state"])
    after_state = validate_role_state(evidence["after_role_state"])
    if before_state["run_id"] != evidence["run_id"] or after_state["run_id"] != evidence["run_id"]:
        raise ProtocolError("INVALID_E2E_ROLE_STATE", "run identity")
    journal = validate_migration_journal(evidence["migration_journal"])
    if not isinstance(evidence["migration_journal_digest"], str) or len(evidence["migration_journal_digest"]) != 64:
        raise ProtocolError("INVALID_E2E_JOURNAL_DIGEST", "migration journal")
    if evidence["migration_journal_digest"] != digest(journal):
        raise ProtocolError("INVALID_E2E_JOURNAL_DIGEST", "migration journal body")
    if journal["run_id"] != evidence["run_id"] or journal["compatibility_row_id"] != evidence["compatibility_row_id"] or journal["compatibility_digest"] != evidence["compatibility_digest"]:
        raise ProtocolError("INVALID_E2E_CORRELATION", "journal run/compatibility")
    if journal["phase"] != evidence["outcome"]:
        raise ProtocolError("INVALID_E2E_CORRELATION", "journal outcome")
    candidate_records = evidence["candidate_ledger_records"]
    if not isinstance(candidate_records, list) or not candidate_records:
        raise ProtocolError("MISSING_CANDIDATE_EVIDENCE", "candidate ledger records")
    validate_ledger_records(candidate_records, allow_session_replacement=True)
    candidate_raw = b"".join(canonical_bytes(record) + b"\n" for record in candidate_records)
    candidate_digest = hashlib.sha256(candidate_raw).hexdigest()
    if candidate_digest == "0" * 64 or journal["candidate_ledger_closed"] is not True or journal["candidate_ledger_digest"] != candidate_digest:
        raise ProtocolError("INVALID_CANDIDATE_LEDGER_DIGEST", candidate_digest)
    terminal_by_key: dict[tuple[Any, ...], str] = {}
    for record in candidate_records:
        terminal_by_key[tuple(record["request_key"])] = record["processing_state"]
    if not terminal_by_key or any(state != "SUCCEEDED" for state in terminal_by_key.values()):
        raise ProtocolError("INCOMPLETE_CANDIDATE_LEDGER", str(terminal_by_key))
    gate_evidence = evidence["gate_evidence"]
    if not isinstance(gate_evidence, dict) or set(gate_evidence) != {"artifact", "observed"}:
        raise ProtocolError("INVALID_GATE_EVIDENCE", "artifact/observed required")
    gate_artifact = validate_compatibility_artifact(gate_evidence["artifact"])
    if digest(gate_artifact) != evidence["compatibility_digest"]:
        raise ProtocolError("INVALID_GATE_EVIDENCE", "compatibility digest")
    for gate_phase in ("PRE_LAUNCH", "POST_LAUNCH", "PROTECTED_MUTATION"):
        decision = gate_compatibility(gate_artifact, gate_evidence["observed"], phase=gate_phase)
        if decision["row_id"] != evidence["compatibility_row_id"] or decision["artifact_digest"] != evidence["compatibility_digest"]:
            raise ProtocolError("INVALID_GATE_EVIDENCE", gate_phase)
    expected_identities = [
        {key: role[key] for key in ("role", "cli_tool", "pane_id", "tab_id", "session_id", "binding_id")}
        for role in after_state["roles"]
    ]
    if evidence["identities"] != expected_identities:
        raise ProtocolError("INVALID_E2E_CORRELATION", "role identities")
    for name, snapshot, state in (
        ("before", evidence["before_bindings"], before_state),
        ("after", evidence["after_bindings"], after_state),
    ):
        if not isinstance(snapshot, list) or len(snapshot) != 3:
            raise ProtocolError("INVALID_E2E_BINDINGS", name)
        expected_bindings = [
            {
                "binding_id": role["binding_id"],
                "parent_binding_id": role["parent_binding_id"],
                "generation": state["leader_generation"],
            }
            for role in state["roles"]
        ]
        if snapshot != expected_bindings:
            raise ProtocolError("INVALID_E2E_CORRELATION", f"{name} bindings")
    timestamp_values = evidence["timestamps"].values() if isinstance(evidence["timestamps"], Mapping) else evidence["timestamps"]
    for value in timestamp_values:
        normalize_timestamp(value)
    for assertion in evidence["assertions"]:
        if set(assertion) != {"name", "passed", "evidence_digest"} or assertion["passed"] is not True:
            raise ProtocolError("FAILED_E2E_ASSERTION", str(assertion.get("name")))
        if len(str(assertion["evidence_digest"])) != 64:
            raise ProtocolError("INVALID_E2E_DIGEST", assertion["name"])
    for identity in evidence["identities"]:
        if set(identity) != {"role", "cli_tool", "pane_id", "tab_id", "session_id", "binding_id"}:
            raise ProtocolError("INVALID_E2E_IDENTITY", str(identity))
    if len({identity["pane_id"] for identity in evidence["identities"]}) != 3:
        raise ProtocolError("INVALID_E2E_TOPOLOGY", "exactly three unique panes required")
    for event in evidence["tool_events"]:
        base = {"operation", "request_id", "response_id", "request_digest", "response_digest"}
        extended = base | {"sequence", "request", "response"}
        if set(event) not in (base, extended):
            raise ProtocolError("INVALID_E2E_PROVENANCE", str(event.get("sequence")))
        if any(not event.get(key) for key in ("operation", "request_id", "response_id")) or any(not isinstance(event[key], str) or len(event[key]) != 64 for key in ("request_digest", "response_digest")):
            raise ProtocolError("INVALID_E2E_PROVENANCE", str(event.get("request_id")))
        if set(event) == extended and (event["request_digest"] != digest(event["request"]) or event["response_digest"] != digest(event["response"])):
            raise ProtocolError("INVALID_E2E_PROVENANCE", str(event.get("sequence")))
    _validate_e2e_trace(evidence, journal, gate_evidence["observed"]["probe_records"])
    envelope_index: dict[str, Mapping[str, Any]] = {}
    candidate_envelopes: list[Mapping[str, Any]] = []
    for envelope in evidence["envelopes"]:
        base = {"message_id", "nonce", "canonical_hash"}
        extended = base | {"envelope"}
        if set(envelope) not in (base, extended) or not isinstance(envelope["canonical_hash"], str) or len(envelope["canonical_hash"]) != 64:
            raise ProtocolError("INVALID_E2E_ENVELOPE", str(envelope))
        if set(envelope) == extended:
            validated_envelope = validate_envelope(envelope["envelope"])
            if envelope["canonical_hash"] != envelope_request_hash(validated_envelope) or envelope["message_id"] != validated_envelope["message_id"] or envelope["nonce"] != validated_envelope["nonce"]:
                raise ProtocolError("INVALID_E2E_ENVELOPE", "summary/body mismatch")
            if validated_envelope["authority_scope"] == "CANDIDATE_VALIDATION":
                candidate_envelopes.append(validated_envelope)
        envelope_index[envelope["message_id"]] = envelope
    before_leader = before_state["roles"][0]
    candidate_context = {
        "migration_id": journal["migration_id"],
        "source_generation": journal["source_generation"],
        "compatibility_digest": journal["compatibility_digest"],
        "old_leader": {key: before_leader[key] for key in ("role", "cli_tool", "session_id", "binding_id")},
        "candidate": {
            **journal["candidate"], "session_id": journal["candidate"]["active_session_id"],
            "cli_tool": "claude",
        },
    }
    validate_candidate_sequence(candidate_envelopes, candidate_context)
    candidate_hashes = {envelope_request_hash(item) for item in candidate_envelopes}
    ledger_hashes = {record["request_sha256"] for record in candidate_records}
    if not candidate_hashes.issubset(ledger_hashes):
        raise ProtocolError("INVALID_CANDIDATE_CORRELATION", "envelope missing from ledger")
    for ack in evidence["acknowledgements"]:
        original_shape = {"message_id", "receipt_id", "completion_id", "echo_hash"}
        extended_shape = {"original_message_id", "nonce", "canonical_hash", "kind"}
        if set(ack) not in (original_shape, extended_shape):
            raise ProtocolError("INVALID_E2E_ACK", str(ack))
        message_id = ack.get("original_message_id", ack.get("message_id"))
        original = envelope_index.get(message_id)
        echoed_hash = ack.get("canonical_hash", ack.get("echo_hash"))
        if original is None or echoed_hash != original["canonical_hash"] or ("nonce" in ack and ack["nonce"] != original["nonce"]) or ("kind" in ack and ack["kind"] not in ("RECEIPT_ACK", "COMPLETION")):
            raise ProtocolError("INVALID_E2E_CORRELATION", str(ack))
    for result in evidence["command_results"]:
        original_shape = {"command", "exit_code", "stdout_digest", "stderr_digest"}
        extended_shape = {"command", "result", "result_digest", "provenance"}
        if set(result) not in (original_shape, extended_shape):
            raise ProtocolError("INVALID_E2E_COMMAND", str(result.get("command")))
        if set(result) == original_shape and (not isinstance(result["exit_code"], int) or any(not isinstance(result[key], str) or len(result[key]) != 64 for key in ("stdout_digest", "stderr_digest"))):
            raise ProtocolError("INVALID_E2E_COMMAND", str(result.get("command")))
        if set(result) == extended_shape and result["result_digest"] != digest(result["result"]):
            raise ProtocolError("INVALID_E2E_COMMAND", str(result.get("command")))
    if not evidence["tool_events"] or not evidence["envelopes"] or not evidence["acknowledgements"] or not evidence["command_results"]:
        raise ProtocolError("MISSING_E2E_PROVENANCE", "tool/envelope/ack/command evidence required")
    return copy.deepcopy(evidence)


def write_e2e_evidence(path: Path, evidence: Mapping[str, Any]) -> str:
    validated = validate_e2e_evidence(evidence)
    raw = canonical_bytes(validated) + b"\n"
    atomic_write(path, raw, backup=True)
    return digest(raw)


LIVE_SMOKE_FIELDS = (
    "schema_version", "run_id", "status", "project_root",
    "compatibility_row_id", "compatibility_digest", "compatibility_artifact",
    "identities", "probe_context", "probe_records", "authority_boundaries",
    "captured_at", "provenance",
)
LIVE_PROBE_FIELDS = (
    "sequence", "primitive", "probe_id", "operation_key", "owner", "target",
    "request", "response", "readback", "hashes", "outcome", "recorded_at",
)
LIVE_CONTEXT_FIELDS = (
    "run_id", "migration_id", "source_generation", "coordinator_binding_id",
    "coordinator_session_id", "evidence_started_at", "evidence_expires_at",
)
DEFERRED_AUTHORITY_BOUNDARIES = {
    "candidate_launch": "DEFERRED_POST_PASS",
    "candidate_ledger": "DEFERRED_POST_PASS",
    "protected_mutation": "DEFERRED_POST_PASS",
    "state_activation": "DEFERRED_POST_PASS",
}
SUPPORTED_PRE_AUTHORITY_PLAN = (
    ("project-resolution", "resolveProject", "leader", "leader", "exact_project_resolution"),
    ("observe-leader", "observeSession", "leader", "leader", "session_identity_is_stable"),
    ("observe-executor", "observeSession", "leader", "executor", "session_identity_is_stable"),
    ("observe-reviewer", "observeSession", "leader", "reviewer", "session_identity_is_stable"),
    ("binding-graph", "reconcile", "leader", "leader", "binding_graph_matches_generation"),
    ("executor-milestone", "updateMilestone", "executor", "executor", "executor_milestone_matches"),
    ("leader-report", "reportToLeader", "executor", "leader", "leader_report_is_queued"),
)


def validate_pre_authority_live_smoke(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate captured CC-Panes observations without claiming authority work."""
    if not isinstance(value, dict) or set(value) != set(LIVE_SMOKE_FIELDS):
        raise ProtocolError("INVALID_LIVE_SMOKE_FIELDS", "exact schema required")
    if value["schema_version"] != 1 or value["status"] != "PRE_AUTHORITY_OBSERVED":
        raise ProtocolError("INVALID_LIVE_SMOKE_STATUS", str(value.get("status")))
    if value["authority_boundaries"] != DEFERRED_AUTHORITY_BOUNDARIES:
        raise ProtocolError("INVALID_LIVE_SMOKE_BOUNDARY", "authority remains deferred")
    normalize_timestamp(value["captured_at"])
    artifact = validate_compatibility_artifact(value["compatibility_artifact"])
    if digest(artifact) != value["compatibility_digest"]:
        raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "compatibility digest")
    rows = [row for row in artifact["rows"] if row["id"] == value["compatibility_row_id"]]
    if len(rows) != 1 or rows[0]["status"] != "SUPPORTED":
        raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "supported row")
    identities = value["identities"]
    identity_fields = {"role", "cli_tool", "pane_id", "tab_id", "session_id", "binding_id"}
    if not isinstance(identities, list) or len(identities) != 3 or [item.get("role") for item in identities] != ["leader", "executor", "reviewer"]:
        raise ProtocolError("INVALID_LIVE_SMOKE_IDENTITIES", "three ordered roles")
    if any(not isinstance(item, dict) or set(item) != identity_fields or any(not item[key] for key in identity_fields) for item in identities):
        raise ProtocolError("INVALID_LIVE_SMOKE_IDENTITIES", "exact non-empty identities")
    if len({item["pane_id"] for item in identities}) != 3 or len({item["binding_id"] for item in identities}) != 3 or len({item["session_id"] for item in identities}) != 3:
        raise ProtocolError("INVALID_LIVE_SMOKE_IDENTITIES", "unique pane/session/binding")
    context = value["probe_context"]
    if not isinstance(context, dict) or set(context) != set(LIVE_CONTEXT_FIELDS) or context["run_id"] != value["run_id"]:
        raise ProtocolError("INVALID_LIVE_SMOKE_CONTEXT", "exact run context")
    started = dt.datetime.fromisoformat(normalize_timestamp(context["evidence_started_at"]).replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(normalize_timestamp(context["evidence_expires_at"]).replace("Z", "+00:00"))
    if started >= expires:
        raise ProtocolError("INVALID_LIVE_SMOKE_CONTEXT", "evidence window")
    coordinator = identities[0]
    if context["coordinator_binding_id"] != coordinator["binding_id"] or context["coordinator_session_id"] != coordinator["session_id"]:
        raise ProtocolError("INVALID_LIVE_SMOKE_CONTEXT", "coordinator identity")
    records = value["probe_records"]
    if not isinstance(records, list) or len(records) != len(SUPPORTED_PRE_AUTHORITY_PLAN):
        raise ProtocolError("MISSING_LIVE_SMOKE_PROBES", "exact supported-row probe set required")
    seen_operations: set[str] = set()
    prior_time = started
    identity_by_binding = {item["binding_id"]: item for item in identities}
    safe_primitives = {
        "resolveProject", "observeSession", "deliverPrompt", "updateMilestone",
        "reportToLeader", "reconcile",
    }
    identity_by_role = {item["role"]: item for item in identities}
    for sequence, (record, definition) in enumerate(zip(records, SUPPORTED_PRE_AUTHORITY_PLAN), 1):
        probe_id, primitive, owner_role, target_role, predicate = definition
        if not isinstance(record, dict) or set(record) != set(LIVE_PROBE_FIELDS) or record["sequence"] != sequence:
            raise ProtocolError("INVALID_LIVE_SMOKE_PROBE", f"sequence {sequence}")
        if (
            record["primitive"] not in safe_primitives
            or (record["probe_id"], record["primitive"]) != (probe_id, primitive)
            or record["outcome"] != "SUCCEEDED"
        ):
            raise ProtocolError("INVALID_LIVE_SMOKE_PROBE", str(record.get("primitive")))
        expected_key = operation_key(context["migration_id"], record["probe_id"], record["primitive"])
        if record["operation_key"] != expected_key or expected_key in seen_operations:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "operation key")
        seen_operations.add(expected_key)
        if not isinstance(record["owner"], dict) or set(record["owner"]) != {"binding_id", "session_id"}:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "owner")
        owner = identity_by_binding.get(record["owner"]["binding_id"])
        if owner is None or owner != identity_by_role[owner_role] or owner["session_id"] != record["owner"]["session_id"]:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "owner identity")
        if not isinstance(record["target"], dict) or set(record["target"]) != {"binding_id", "session_id"}:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "target")
        target = identity_by_binding.get(record["target"]["binding_id"])
        if target is None or target != identity_by_role[target_role] or target["session_id"] != record["target"]["session_id"]:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "target identity")
        if not isinstance(record["readback"], dict) or set(record["readback"]) != {"predicate", "outcome", "value"} or record["readback"]["predicate"] != predicate or record["readback"]["outcome"] != "COMPLETE" or record["readback"]["value"] != record["response"]:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "readback")
        hashes = record["hashes"]
        if not isinstance(hashes, dict) or hashes != {
            "request_sha256": digest(record["request"]),
            "response_sha256": digest(record["response"]),
            "readback_sha256": digest(record["readback"]["value"]),
        }:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "hashes")
        recorded = dt.datetime.fromisoformat(normalize_timestamp(record["recorded_at"]).replace("Z", "+00:00"))
        if recorded < prior_time or recorded > expires:
            raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "record time")
        prior_time = recorded
        if primitive == "resolveProject":
            if record["request"] != {"project_root": value["project_root"]} or record["response"] != {"project_root": value["project_root"]}:
                raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "project resolution")
        elif primitive == "observeSession":
            expected_identity = identity_by_role[target_role]
            if record["request"] != {"session_id": expected_identity["session_id"]} or record["response"] != expected_identity:
                raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "session observation")
        elif primitive == "reconcile":
            expected_graph = [
                {
                    "binding_id": item["binding_id"],
                    "parent_binding_id": None if item["role"] == "leader" else coordinator["binding_id"],
                }
                for item in identities
            ]
            if record["request"] != {"binding_ids": [item["binding_id"] for item in identities]} or record["response"] != {"bindings": expected_graph}:
                raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "binding graph")
        elif primitive == "updateMilestone":
            if record["request"].get("binding_id") != identity_by_role["executor"]["binding_id"] or record["response"] != record["request"]:
                raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "executor milestone")
        elif primitive == "reportToLeader":
            if record["request"].get("leader_binding_id") != coordinator["binding_id"] or record["request"].get("executor_binding_id") != identity_by_role["executor"]["binding_id"] or record["response"] != {"operation_key": expected_key, "queued": True}:
                raise ProtocolError("INVALID_LIVE_SMOKE_CORRELATION", "leader report")
    if not isinstance(value["provenance"], dict) or not value["provenance"]:
        raise ProtocolError("MISSING_LIVE_SMOKE_PROVENANCE", "capture provenance")
    return copy.deepcopy(dict(value))


def build_pre_authority_live_smoke(
    *, run_id: str, project_root: Path, compatibility_artifact: Mapping[str, Any],
    compatibility_row_id: str, identities: list[Mapping[str, Any]],
    probe_context: Mapping[str, Any], probe_records: list[Mapping[str, Any]],
    captured_at: str, provenance: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = validate_compatibility_artifact(compatibility_artifact)
    value = {
        "schema_version": 1, "run_id": run_id, "status": "PRE_AUTHORITY_OBSERVED",
        "project_root": str(project_root.resolve()),
        "compatibility_row_id": compatibility_row_id,
        "compatibility_digest": digest(artifact),
        "compatibility_artifact": artifact, "identities": copy.deepcopy(identities),
        "probe_context": copy.deepcopy(dict(probe_context)),
        "probe_records": copy.deepcopy(probe_records),
        "authority_boundaries": copy.deepcopy(DEFERRED_AUTHORITY_BOUNDARIES),
        "captured_at": normalize_timestamp(captured_at),
        "provenance": copy.deepcopy(dict(provenance)),
    }
    return validate_pre_authority_live_smoke(value)


def write_pre_authority_live_smoke(path: Path, value: Mapping[str, Any]) -> str:
    validated = validate_pre_authority_live_smoke(value)
    raw = canonical_bytes(validated) + b"\n"
    atomic_write(path, raw, backup=True)
    return hashlib.sha256(raw).hexdigest()


def build_e2e_evidence(
    *, run_id: str, transport: FakeTransport, compatibility_artifact: Mapping[str, Any],
    compatibility_row_id: str, before_role_state: Mapping[str, Any],
    after_role_state: Mapping[str, Any], before_bindings: list[Mapping[str, Any]],
    after_bindings: list[Mapping[str, Any]], migration_journal: Mapping[str, Any],
    candidate_ledger_records: list[Mapping[str, Any]], gate_observed: Mapping[str, Any],
    envelopes: list[Mapping[str, Any]], probe_results: list[Mapping[str, Any]],
    assertions: list[Mapping[str, Any]], command_records: list[Mapping[str, Any]],
    timestamps: list[str], outcome: str,
) -> dict[str, Any]:
    """Derive a tamper-evident schema-1 E2E artifact from captured raw evidence."""
    artifact = validate_compatibility_artifact(compatibility_artifact)
    matching = [row for row in artifact["rows"] if row["id"] == compatibility_row_id]
    if len(matching) != 1 or matching[0]["status"] != "SUPPORTED":
        raise ProtocolError("INVALID_E2E_COMPATIBILITY_ROW", compatibility_row_id)
    if not isinstance(after_role_state.get("roles"), list) or len(after_role_state["roles"]) != 3:
        raise ProtocolError("INVALID_E2E_ROLE_STATE", "three after-state roles required")
    identities = []
    for role in after_role_state["roles"]:
        identities.append({key: role[key] for key in ("role", "cli_tool", "pane_id", "tab_id", "session_id", "binding_id")})
    tool_events = [
        {key: call[key] for key in ("sequence", "operation", "request_id", "response_id", "request", "response", "request_digest", "response_digest")}
        for call in transport.calls
    ]
    envelope_summaries = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for raw in envelopes:
        envelope = validate_envelope(raw)
        by_id[envelope["message_id"]] = envelope
        envelope_summaries.append({
            "message_id": envelope["message_id"], "nonce": envelope["nonce"],
            "canonical_hash": envelope_request_hash(envelope), "envelope": envelope,
        })
    acknowledgements = []
    for envelope in by_id.values():
        if envelope["message_type"] not in ("RECEIPT_ACK", "COMPLETION") or not envelope["reply_to"]:
            continue
        original = by_id.get(envelope["reply_to"])
        if original is None:
            raise ProtocolError("INVALID_E2E_CORRELATION", envelope["message_id"])
        acknowledgements.append({
            "original_message_id": original["message_id"], "nonce": original["nonce"],
            "canonical_hash": envelope_request_hash(original), "kind": envelope["message_type"],
        })
    derived_probes = []
    for probe in probe_results:
        evidence_value = probe.get("evidence")
        evidence_digest = probe.get("evidence_digest") or digest(evidence_value)
        derived_probes.append({"probe_id": probe["probe_id"], "owner": probe["owner"], "passed": probe["passed"], "evidence_digest": evidence_digest})
    derived_assertions = []
    for assertion in assertions:
        evidence_value = assertion.get("evidence")
        evidence_digest = assertion.get("evidence_digest") or digest(evidence_value)
        derived_assertions.append({"name": assertion["name"], "passed": assertion["passed"], "evidence_digest": evidence_digest})
    command_results = []
    for record in command_records:
        result = {key: record[key] for key in ("exit_code", "stdout", "stderr")}
        command_results.append({"command": record["command"], "result": result, "result_digest": digest(result), "provenance": copy.deepcopy(record["provenance"])})
    evidence = {
        "schema_version": 1, "run_id": run_id, "protocol_version": "0.2.0",
        "plugin_version": "0.2.0",
        "schema_versions": {"role_state": 1, "migration": 1, "envelope": 1},
        "compatibility_row_id": compatibility_row_id,
        "compatibility_digest": digest(artifact),
        "before_role_state": copy.deepcopy(before_role_state),
        "after_role_state": copy.deepcopy(after_role_state),
        "before_bindings": copy.deepcopy(before_bindings),
        "after_bindings": copy.deepcopy(after_bindings), "identities": identities,
        "tool_events": tool_events, "migration_journal": copy.deepcopy(migration_journal),
        "migration_journal_digest": digest(migration_journal),
        "candidate_ledger_records": copy.deepcopy(candidate_ledger_records),
        "gate_evidence": {"artifact": copy.deepcopy(artifact), "observed": copy.deepcopy(gate_observed)},
        "envelopes": envelope_summaries, "acknowledgements": acknowledgements,
        "timestamps": [normalize_timestamp(item) for item in timestamps],
        "probe_results": derived_probes, "assertions": derived_assertions,
        "command_results": command_results, "outcome": outcome,
    }
    return validate_e2e_evidence(evidence)


def build_and_write_e2e(path: Path, **captured: Any) -> tuple[dict[str, Any], str]:
    """Build, validate, and atomically write captured E2E evidence."""
    evidence = build_e2e_evidence(**captured)
    return evidence, write_e2e_evidence(path, evidence)


def utc_now() -> str:
    return normalize_timestamp(dt.datetime.now(dt.timezone.utc).isoformat())


def _fake_role_state(root: Path, run_id: str, migration_id: str, generation: int, leader_cli: str, phase: str) -> dict[str, Any]:
    capabilities = sorted(ADAPTER_OPERATIONS)
    leader_binding = "binding-leader-old" if generation == 1 else "binding-leader-new"
    roles = []
    for index, role in enumerate(("leader", "executor", "reviewer")):
        cli_tool = leader_cli if role == "leader" else "codex"
        suffix = "old" if role == "leader" and generation == 1 else "new" if role == "leader" else role
        roles.append({
            "schema_version": 1, "role": role, "cli_tool": cli_tool,
            "adapter_id": "fake-ccpanes", "runtime_kind": "local",
            "project_root": str(root), "session_id": f"session-{suffix}",
            "resume_id": None, "pane_id": f"pane-{role}", "tab_id": f"tab-{suffix}",
            "binding_id": leader_binding if role == "leader" else f"binding-{role}",
            "parent_binding_id": None if role == "leader" else leader_binding,
            "leader_generation": generation,
            "authority_state": "ACTIVE" if role == "leader" else "FROZEN",
            "required_capabilities": capabilities, "advertised_capabilities": capabilities,
            "verified_at": "2026-07-26T12:00:00Z",
        })
    return validate_role_state({
        "schema_version": 1, "workflow_version": "0.2.0", "run_id": run_id,
        "leader_generation": generation, "migration_id": migration_id,
        "migration_phase": phase, "roles": roles,
    })


def _fake_envelope(*, run_id: str, message_id: str, message_type: str, sender: Mapping[str, Any], recipient: Mapping[str, Any], nonce: str, payload: Mapping[str, Any], reply_to: str | None = None) -> dict[str, Any]:
    value = {
        "schema_version": 1, "authority_scope": "ROLE", "message_id": message_id,
        "reply_to": reply_to, "run_id": run_id, "migration_id": None,
        "leader_generation": 2, "message_type": message_type,
        "sender": dict(sender), "recipient": dict(recipient), "nonce": nonce,
        "created_at": "2026-07-26T12:00:00Z", "expires_at": "2026-07-26T13:00:00Z",
        "payload": dict(payload), "payload_path": None, "payload_sha256": digest(payload),
    }
    return validate_envelope(value)


def _fake_activation_envelope(
    *, run_id: str, migration_id: str, message_id: str,
    sender: Mapping[str, Any], recipient: Mapping[str, Any], nonce: str,
    leader_generation: int, leader_binding_id: str,
    executor_parent_binding_id: str, reviewer_parent_binding_id: str,
    roles_sha256: str,
) -> dict[str, Any]:
    payload = {
        "migration_id": migration_id, "leader_generation": leader_generation,
        "leader_binding_id": leader_binding_id,
        "executor_parent_binding_id": executor_parent_binding_id,
        "reviewer_parent_binding_id": reviewer_parent_binding_id,
        "roles_sha256": roles_sha256,
    }
    return validate_envelope({
        "schema_version": 1, "authority_scope": "ROLE", "message_id": message_id,
        "reply_to": None, "run_id": run_id, "migration_id": migration_id,
        "leader_generation": leader_generation, "message_type": "LEADER_ACTIVATED",
        "sender": dict(sender), "recipient": dict(recipient), "nonce": nonce,
        "created_at": "2026-07-26T12:00:00Z", "expires_at": "2026-07-26T13:00:00Z",
        "payload": payload, "payload_path": None, "payload_sha256": digest(payload),
    })


def _fake_activation_receipt(
    *, request: Mapping[str, Any], sender: Mapping[str, Any],
    recipient: Mapping[str, Any], operation_key_value: str,
) -> dict[str, Any]:
    payload = {
        "original_message_id": request["message_id"],
        "original_nonce": request["nonce"],
        "canonical_request_hash": envelope_request_hash(request),
        "accepted": True, "operation_key": operation_key_value,
    }
    return validate_envelope({
        "schema_version": 1, "authority_scope": "ROLE",
        "message_id": request["message_id"] + "-receipt",
        "reply_to": request["message_id"], "run_id": request["run_id"],
        "migration_id": request["migration_id"],
        "leader_generation": request["leader_generation"],
        "message_type": "RECEIPT_ACK", "sender": dict(sender),
        "recipient": dict(recipient), "nonce": request["nonce"],
        "created_at": "2026-07-26T12:00:00Z", "expires_at": "2026-07-26T13:00:00Z",
        "payload": payload, "payload_path": None, "payload_sha256": digest(payload),
    })


def _fake_candidate_envelope(
    *, run_id: str, migration_id: str, message_id: str, message_type: str,
    sender: Mapping[str, Any], recipient: Mapping[str, Any], nonce: str,
    payload: Mapping[str, Any], reply_to: str | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": 1, "authority_scope": "CANDIDATE_VALIDATION",
        "message_id": message_id, "reply_to": reply_to, "run_id": run_id,
        "migration_id": migration_id, "leader_generation": 1,
        "message_type": message_type, "sender": dict(sender),
        "recipient": dict(recipient), "nonce": nonce,
        "created_at": "2026-07-26T12:00:00Z",
        "expires_at": "2026-07-26T13:00:00Z", "payload": dict(payload),
        "payload_path": None, "payload_sha256": digest(payload),
    }
    return validate_envelope(value)


def _gate_probe_record(
    *, sequence: int, gate_phase: str, owner: str, primitive: str,
    predicate: str, probe_id: str, context: Mapping[str, Any],
    row_id: str, artifact_digest: str, expected: Mapping[str, Any],
    target_kind: str, target_id: str,
) -> dict[str, Any]:
    if owner == "coordinator":
        owner_binding = context["coordinator_binding_id"]
        owner_session = context["coordinator_session_id"]
    else:
        owner_binding = context["candidate_binding_id"]
        owner_session = context["candidate_session_id"]
    if not isinstance(target_kind, str) or not target_kind or not isinstance(target_id, str) or not target_id:
        raise ProtocolError("INVALID_GATE_EVIDENCE", "target kind/id")
    if target_kind in ("leader-pane", "layout"):
        target_binding = target_session = None
    elif target_kind in ("coordinator-binding", "coordinator-session"):
        target_binding = context["coordinator_binding_id"]
        target_session = context["coordinator_session_id"]
    elif target_kind in ("candidate-binding", "candidate-session"):
        target_binding = context["candidate_binding_id"]
        target_session = context["candidate_session_id"]
    else:
        raise ProtocolError("INVALID_GATE_EVIDENCE", "target kind")
    actual = copy.deepcopy(dict(expected))
    readback = {"predicate": predicate, "outcome": "COMPLETE", "value": copy.deepcopy(actual)}
    return {
        "sequence": sequence, "gate_phase": gate_phase, "owner": owner,
        "primitive": primitive, "target": {"kind": target_kind, "id": target_id},
        "expected": copy.deepcopy(dict(expected)), "actual": actual,
        "cleanup": {
            "required": False, "completed": True,
            "evidence_sha256": digest({"required": False, "completed": True}),
        },
        "probe_id": probe_id,
        "operation_key": operation_key(context["migration_id"], probe_id, primitive),
        "identities": {
            "run_id": context["run_id"], "migration_id": context["migration_id"],
            "source_generation": context["source_generation"],
            "target_generation": context["target_generation"],
            "owner_binding_id": owner_binding, "owner_session_id": owner_session,
            "target_binding_id": target_binding, "target_session_id": target_session,
        },
        "hashes": {
            "expected_sha256": digest(expected), "actual_sha256": digest(actual),
            "readback_sha256": digest(readback["value"]),
        },
        "readback": readback, "compatibility_row_id": row_id,
        "compatibility_digest": artifact_digest, "outcome": "SUCCEEDED",
        "recorded_at": f"2026-07-26T12:00:{sequence:02d}Z",
    }


def _supported_gate_observed(
    artifact: Mapping[str, Any], row_id: str, context: Mapping[str, Any],
) -> dict[str, Any]:
    row = next(item for item in artifact["rows"] if item["id"] == row_id)
    artifact_digest = digest(artifact)
    definitions = (
        ("PRE_LAUNCH", "coordinator", "launchRole", "launch_creates_one_tab_in_existing_pane", "pre-launch-role", {"pane_id": "pane-leader", "new_tab_count": 1, "new_pane_count": 0}, "leader-pane", "pane-leader"),
        ("PRE_LAUNCH", "coordinator", "observeSession", "session_identity_is_stable", "pre-observe-coordinator", {"session_id": context["coordinator_session_id"], "stable": True}, "coordinator-session", context["coordinator_session_id"]),
        ("POST_LAUNCH", "coordinator", "bindRole", "binding_has_exact_candidate_metadata", "post-bind-candidate", {"binding_id": context["candidate_binding_id"], "generation": context["target_generation"], "authority_state": "CANDIDATE"}, "candidate-binding", context["candidate_binding_id"]),
        ("POST_LAUNCH", "coordinator", "deliverPrompt", "prompt_output_has_probe_id_nonce_hash", "post-deliver-handshake", {"session_id": context["candidate_session_id"], "delivered": True}, "candidate-session", context["candidate_session_id"]),
        ("POST_LAUNCH", "candidate", "reportToLeader", "report_observation_has_operation_key_probe_id_hash", "post-report-completion", {"binding_id": context["candidate_binding_id"], "observed": True}, "coordinator-binding", context["coordinator_binding_id"]),
        ("POST_LAUNCH", "coordinator", "observeSession", "session_identity_is_stable", "post-observe-candidate", {"session_id": context["candidate_session_id"], "stable": True}, "candidate-session", context["candidate_session_id"]),
    )
    records = [
        _gate_probe_record(
            sequence=index, gate_phase=phase, owner=owner, primitive=primitive,
            predicate=predicate, probe_id=probe_id, context=context,
            row_id=row_id, artifact_digest=artifact_digest, expected=expected,
            target_kind=target_kind, target_id=target_id,
        )
        for index, (phase, owner, primitive, predicate, probe_id, expected, target_kind, target_id)
        in enumerate(definitions, 1)
    ]
    return {
        **{key: row[key] for key in (
            "os", "architecture", "runtime_kind", "shell", "claude_version",
            "codex_version", "plugin_version", "role_state_schema",
            "migration_schema", "envelope_schema",
        )},
        "install_source": row["install_sources"][0], "cache_refreshed": True,
        "fresh_sessions": True, "static": copy.deepcopy(row["required_static"]),
        "capabilities": copy.deepcopy(row["required_capabilities"]),
        "predicates": copy.deepcopy(row["required_predicates"]),
        "probe_context": copy.deepcopy(dict(context)), "probe_records": records,
    }


def run_supported_fake_e2e(root: Path, run_id: str, compatibility_path: Path) -> dict[str, Any]:
    """Run one chronological keyed fake migration and freeze its observed trace."""
    root = root.resolve()
    migration_id = run_id + "-claude-leader"
    artifact = validate_compatibility_artifact(json.loads(compatibility_path.read_text(encoding="utf-8")))
    row_id = "windows-local-claude-2.1.220-codex-0.144.6"
    artifact_digest = digest(artifact)
    before = _fake_role_state(root, run_id, migration_id, 1, "codex", "CREATED")
    activation_state = _fake_role_state(root, run_id, migration_id, 2, "claude", "STATE_ACTIVATED")
    after = _fake_role_state(root, run_id, migration_id, 2, "claude", "COMMITTED")
    before_bindings = [{"binding_id": role["binding_id"], "parent_binding_id": role["parent_binding_id"], "generation": 1} for role in before["roles"]]
    after_bindings = [{"binding_id": role["binding_id"], "parent_binding_id": role["parent_binding_id"], "generation": 2} for role in after["roles"]]
    old_leader, new_leader = before["roles"][0], after["roles"][0]
    old_party = {key: old_leader[key] for key in ("role", "cli_tool", "session_id", "binding_id")}
    leader_party = {key: new_leader[key] for key in ("role", "cli_tool", "session_id", "binding_id")}
    transport = FakeTransport()
    transport.projects[str(root)] = {"project_root": str(root)}
    for role in before["roles"]:
        transport.sessions[role["session_id"]] = {key: role[key] for key in ("role", "cli_tool", "session_id", "pane_id", "tab_id")} | {"running": True}
        transport.bindings[role["binding_id"]] = {
            "role": role["role"], "binding_id": role["binding_id"],
            "parent_binding_id": role["parent_binding_id"], "leader_generation": 1,
            "authority_state": role["authority_state"],
        }
    transport.configure_migration_oracle(
        pane_ids=[role["pane_id"] for role in before["roles"]],
        source_generation=1, target_generation=2,
        old_leader_binding_id=old_leader["binding_id"],
        new_leader_binding_id=new_leader["binding_id"],
        old_leader_session_id=old_leader["session_id"],
        worker_binding_ids=[role["binding_id"] for role in before["roles"][1:]],
        candidate_binding_ids=[new_leader["binding_id"]],
        recoverable_parent_ids=[old_leader["binding_id"], new_leader["binding_id"]],
        role_state=before,
    )
    scratch = tempfile.TemporaryDirectory(prefix="agent-team-e2e-")
    temp_root = Path(scratch.name)
    journal_helper = MigrationJournal(temp_root / "migration.json")
    journal_helper.write({
        "schema_version": 1, "run_id": run_id, "migration_id": migration_id,
        "phase": "CREATED", "source_generation": 1, "target_generation": 2,
        "old_leader": {"binding_id": old_leader["binding_id"], "session_id": old_leader["session_id"]},
        "candidate": {"binding_id": new_leader["binding_id"], "active_session_id": new_leader["session_id"], "resume_lineage": "fake-claude-lineage"},
        "compatibility_row_id": row_id, "compatibility_digest": artifact_digest,
        "before_state": before,
        "expected_binding_parents": {role["binding_id"]: new_leader["binding_id"] for role in after["roles"][1:]},
        "observed_binding_parents": {}, "steps": [], "recovery_decision": None,
        "candidate_ledger_digest": None, "candidate_ledger_closed": False,
    })
    clock = 0

    def next_time() -> str:
        nonlocal clock
        clock += 1
        return f"2026-07-26T12:{clock // 60:02d}:{clock % 60:02d}Z"

    def begin(phase: str, key: str) -> None:
        journal_helper.record_step(phase, key, "BEFORE", {"operation_key": key}, None, next_time())

    def applied(phase: str, key: str) -> None:
        journal_helper.record_step(phase, key, "APPLIED", None, {"operation_key": key}, next_time())

    def paired(phase: str, key: str, effect: Callable[[], Any]) -> Any:
        begin(phase, key)
        result = effect()
        applied(phase, key)
        return result

    gate_context = {
        "run_id": run_id, "migration_id": migration_id, "source_generation": 1,
        "target_generation": 2, "coordinator_binding_id": old_leader["binding_id"],
        "coordinator_session_id": old_leader["session_id"],
        "candidate_binding_id": new_leader["binding_id"], "candidate_session_id": new_leader["session_id"],
        "evidence_started_at": "2026-07-26T12:00:00Z", "evidence_expires_at": "2026-07-26T13:00:00Z",
    }
    gate_records: list[dict[str, Any]] = []

    def capture_gate(
        *, phase: str, owner: str, primitive: str, predicate: str, probe_id: str,
        target_kind: str, target_id: str, effect: Callable[[str], Any],
    ) -> None:
        key = operation_key(migration_id, probe_id, primitive)
        begin("PREFLIGHTED" if phase == "PRE_LAUNCH" else "VALIDATED", key)
        effect(key)
        call = transport.calls[-1]
        gate_records.append(_gate_probe_record(
            sequence=len(gate_records) + 1, gate_phase=phase, owner=owner,
            primitive=primitive, predicate=predicate, probe_id=probe_id,
            context=gate_context, row_id=row_id, artifact_digest=artifact_digest,
            expected=call["response"], target_kind=target_kind, target_id=target_id,
        ))
        if phase == "PRE_LAUNCH":
            applied("PREFLIGHTED", key)

    old_descriptor = {key: old_leader[key] for key in ("role", "cli_tool", "session_id", "pane_id", "tab_id")}
    capture_gate(
        phase="PRE_LAUNCH", owner="coordinator", primitive="launchRole",
        predicate="launch_creates_one_tab_in_existing_pane", probe_id="pre-launch-role",
        target_kind="leader-pane", target_id=old_leader["pane_id"],
        effect=lambda key: transport.launchRole(key, old_descriptor),
    )
    capture_gate(
        phase="PRE_LAUNCH", owner="coordinator", primitive="observeSession",
        predicate="session_identity_is_stable", probe_id="pre-observe-coordinator",
        target_kind="coordinator-session", target_id=old_leader["session_id"],
        effect=lambda key: transport.observeSession(old_leader["session_id"], key=key),
    )
    gate_observed = _supported_gate_observed(artifact, row_id, gate_context)
    gate_observed["probe_records"] = copy.deepcopy(gate_records)
    gate_compatibility(artifact, gate_observed, phase="PRE_LAUNCH")

    candidate_launch_key = operation_key(migration_id, "candidate-launch", "launchRole")
    paired("PREPARED", candidate_launch_key, lambda: transport.launchRole(candidate_launch_key, {
        "role": "leader", "cli_tool": "claude", "session_id": new_leader["session_id"],
        "pane_id": new_leader["pane_id"], "tab_id": new_leader["tab_id"],
    }))

    candidate_probe_ids = ["candidate-binding", "candidate-prompt", "candidate-report"]
    candidate_operation_keys = {
        "candidate-binding": operation_key(migration_id, "candidate-binding", "bindRole"),
        "candidate-prompt": operation_key(migration_id, "candidate-prompt", "deliverPrompt"),
        "candidate-report": operation_key(migration_id, "candidate-report", "reportToLeader"),
    }
    candidate_nonce = "nonce-candidate-validation"
    candidate_handshake = _fake_candidate_envelope(
        run_id=run_id, migration_id=migration_id,
        message_id="candidate-handshake", message_type="HANDSHAKE",
        sender=old_party, recipient=leader_party, nonce=candidate_nonce,
        payload={
            "probe_id": "candidate-validation", "target_generation": 2,
            "compatibility_row_id": row_id, "compatibility_digest": digest(artifact),
            "candidate_binding_id": new_leader["binding_id"],
            "candidate_session_id": new_leader["session_id"],
            "candidate_resume_lineage": "fake-claude-lineage",
            "handoff_digest": digest({"run_id": run_id, "source_generation": 1}),
            "ordered_probe_ids": candidate_probe_ids,
            "operation_keys": candidate_operation_keys,
        },
    )
    candidate_hash = envelope_request_hash(candidate_handshake)
    common_echo = {
        "original_message_id": candidate_handshake["message_id"],
        "original_nonce": candidate_nonce, "canonical_request_hash": candidate_hash,
        "migration_id": migration_id, "probe_id": "candidate-validation",
    }
    candidate_receipt = _fake_candidate_envelope(
        run_id=run_id, migration_id=migration_id,
        message_id="candidate-receipt", message_type="RECEIPT_ACK",
        sender=leader_party, recipient=old_party, nonce=candidate_nonce,
        payload={**common_echo, "candidate_identity": leader_party},
        reply_to=candidate_handshake["message_id"],
    )
    candidate_probe_evidence = {
        "candidate-binding": {"binding_id": new_leader["binding_id"], "authority_state": "CANDIDATE", "generation": 2},
        "candidate-prompt": {"session_id": new_leader["session_id"], "nonce": candidate_nonce, "canonical_hash": candidate_hash},
        "candidate-report": {"operation_key": candidate_operation_keys["candidate-report"], "probe_id": "candidate-validation", "canonical_hash": candidate_hash},
    }
    candidate_completion = _fake_candidate_envelope(
        run_id=run_id, migration_id=migration_id,
        message_id="candidate-completion", message_type="COMPLETION",
        sender=leader_party, recipient=old_party, nonce=candidate_nonce,
        payload={
            **common_echo,
            "probes": [
                {
                    "probe_id": probe_id,
                    "operation_key": candidate_operation_keys[probe_id],
                    "effect_policy": EffectPolicy.RECONCILABLE_IDEMPOTENT.value,
                    "terminal_outcome": "SUCCEEDED",
                    "evidence_digest": digest(candidate_probe_evidence[probe_id]),
                }
                for probe_id in candidate_probe_ids
            ],
            "candidate_binding_readback": {"binding_id": new_leader["binding_id"]},
            "report_delivery_readback": {"operation_key": candidate_operation_keys["candidate-report"]},
        },
        reply_to=candidate_handshake["message_id"],
    )
    candidate_context = {
        "migration_id": migration_id, "source_generation": 1,
        "compatibility_digest": digest(artifact), "old_leader": old_party,
        "candidate": {
            "binding_id": new_leader["binding_id"], "session_id": new_leader["session_id"],
            "active_session_id": new_leader["session_id"],
            "resume_lineage": "fake-claude-lineage", "cli_tool": "claude",
        },
    }
    candidate_sequence = [candidate_handshake, candidate_receipt, candidate_completion]
    validate_candidate_sequence(candidate_sequence, candidate_context)
    envelopes: list[Mapping[str, Any]] = list(candidate_sequence)
    probe_results: list[Mapping[str, Any]] = []
    candidate_path = temp_root / "candidate.jsonl"
    candidate_ledger = CandidateLedger(candidate_path, journal_helper.load, new_leader["binding_id"], new_leader["session_id"], 2)

    def record_effect(
        envelope: Mapping[str, Any], key: str, desired: Mapping[str, Any],
        execute: Callable[[], Any], predicate: str,
    ) -> None:
        candidate_ledger.append_envelope(envelope, "RECEIVED", candidate_context, recorded_at=next_time())
        executing = candidate_ledger.append_envelope(
            envelope, "EXECUTING", candidate_context, operation_key=key,
            effect_policy=EffectPolicy.RECONCILABLE_IDEMPOTENT,
            readback_predicate=predicate, recorded_at=next_time(),
        )
        execute()
        recover_executing(candidate_ledger, executing, Observation(transport.observe_effect(key, desired)), adapter_honors_key=True, recorded_at=next_time())

    def record_probe_effect(
        probe_id: str, key: str, desired: Mapping[str, Any],
        execute: Callable[[], Any], predicate: str,
    ) -> None:
        request_key = (old_leader["binding_id"], run_id, 1, "probe-" + probe_id)
        request_hash = digest(desired)
        candidate_ledger.append(request_key, request_hash, "RECEIVED", recorded_at=next_time())
        executing = candidate_ledger.append(
            request_key, request_hash, "EXECUTING", operation_key=key,
            effect_policy=EffectPolicy.RECONCILABLE_IDEMPOTENT,
            readback_predicate=predicate, recorded_at=next_time(),
        )
        execute()
        recover_executing(candidate_ledger, executing, Observation(transport.observe_effect(key, desired)), adapter_honors_key=True, recorded_at=next_time())

    bind_desired = {"role": "leader", "binding_id": new_leader["binding_id"], "parent_binding_id": None, "leader_generation": 2, "authority_state": "CANDIDATE"}
    capture_gate(
        phase="POST_LAUNCH", owner="coordinator", primitive="bindRole",
        predicate="binding_has_exact_candidate_metadata", probe_id="candidate-binding",
        target_kind="candidate-binding", target_id=new_leader["binding_id"],
        effect=lambda key: record_probe_effect("candidate-binding", key, bind_desired, lambda: transport.bindRole(key, bind_desired), "binding_has_exact_candidate_metadata"),
    )

    handshake_desired = {
        "session_id": new_leader["session_id"], "probe_id": "candidate-validation",
        "nonce": candidate_nonce, "request_hash": candidate_hash,
    }
    capture_gate(
        phase="POST_LAUNCH", owner="coordinator", primitive="deliverPrompt",
        predicate="prompt_output_has_probe_id_nonce_hash", probe_id="candidate-prompt",
        target_kind="candidate-session", target_id=new_leader["session_id"],
        effect=lambda key: record_effect(candidate_handshake, key, handshake_desired, lambda: transport.deliverPrompt(key, new_leader["session_id"], "candidate-validation", candidate_nonce, candidate_hash), "prompt_output_has_probe_id_nonce_hash"),
    )
    receipt_key = operation_key(migration_id, "candidate-receipt", "reportToLeader")
    receipt_report = {"operation_key": receipt_key, "probe_id": "candidate-validation", "canonical_hash": candidate_hash, "kind": "RECEIPT_ACK"}
    record_effect(candidate_receipt, receipt_key, receipt_report, lambda: transport.reportToLeader(receipt_key, receipt_report), "report_observation_has_operation_key_probe_id_hash")
    report_desired = candidate_probe_evidence["candidate-report"]
    capture_gate(
        phase="POST_LAUNCH", owner="candidate", primitive="reportToLeader",
        predicate="report_observation_has_operation_key_probe_id_hash", probe_id="candidate-report",
        target_kind="coordinator-binding", target_id=old_leader["binding_id"],
        effect=lambda key: record_probe_effect("candidate-report", key, report_desired, lambda: transport.reportToLeader(key, report_desired), "report_observation_has_operation_key_probe_id_hash"),
    )
    capture_gate(
        phase="POST_LAUNCH", owner="coordinator", primitive="observeSession",
        predicate="session_identity_is_stable", probe_id="post-observe-candidate",
        target_kind="candidate-session", target_id=new_leader["session_id"],
        effect=lambda key: transport.observeSession(new_leader["session_id"], key=key),
    )
    completion_key = operation_key(migration_id, "candidate-completion", "reportToLeader")
    completion_report = {"operation_key": completion_key, "probe_id": "candidate-validation", "canonical_hash": candidate_hash, "kind": "COMPLETION"}
    record_effect(candidate_completion, completion_key, completion_report, lambda: transport.reportToLeader(completion_key, completion_report), "report_observation_has_operation_key_probe_id_hash")
    post_keys = [record["operation_key"] for record in gate_records if record["gate_phase"] == "POST_LAUNCH"]
    for key in post_keys:
        applied("VALIDATED", key)
    gate_observed["probe_records"] = copy.deepcopy(gate_records)
    gate_compatibility(artifact, gate_observed, phase="POST_LAUNCH")
    candidate_records = candidate_ledger.records()
    exact_candidate_digest = journal_helper.close_candidate_ledger(candidate_path, recovery_decision="COMMIT")
    if not candidate_records or exact_candidate_digest == "0" * 64:
        raise ProtocolError("MISSING_CANDIDATE_EVIDENCE", "empty candidate ledger")

    transport.configure_gate_evidence(artifact, gate_observed)
    coordinator = FakeMigrationCoordinator(transport, journal_helper)
    registered = {
        "role": "leader", "binding_id": new_leader["binding_id"],
        "parent_binding_id": None, "leader_generation": 2,
        "authority_state": "CANDIDATE",
    }
    register_key = operation_key(migration_id, "new-leader-registered", "bindRole")
    paired(
        "NEW_LEADER_REGISTERED", register_key,
        lambda: coordinator.protected_mutation(
            "register_new_leader", lambda: transport.bindRole(register_key, registered),
        ),
    )
    for phase, mutation, worker in (
        ("EXECUTOR_REPARENTED", "reparent_executor", after["roles"][1]),
        ("REVIEWER_REPARENTED", "reparent_reviewer", after["roles"][2]),
    ):
        key = operation_key(migration_id, phase.lower(), "bindRole")
        desired = {
            "role": worker["role"], "binding_id": worker["binding_id"],
            "parent_binding_id": new_leader["binding_id"],
            "leader_generation": 2, "authority_state": "FROZEN",
        }
        paired(
            phase, key,
            lambda mutation=mutation, key=key, desired=desired: coordinator.protected_mutation(
                mutation, lambda: transport.bindRole(key, desired),
            ),
        )
    activation_key = operation_key(migration_id, "state-activated", "reconcile")
    activation_readback = paired(
        "STATE_ACTIVATED", activation_key,
        lambda: coordinator.protected_mutation(
            "activate_state", lambda: transport.activateRoleState(activation_key, activation_state),
        ),
    )
    if activation_readback != activation_state or transport.role_state_readback != activation_state:
        raise ProtocolError("MISSING_ROLE_STATE_READBACK", "STATE_ACTIVATED")

    activation_roles_digest = digest(activation_state["roles"])
    for worker in activation_state["roles"][1:]:
        worker_party = {key: worker[key] for key in ("role", "cli_tool", "session_id", "binding_id")}
        role = worker["role"]
        nonce = f"nonce-activation-{role}"
        request = _fake_activation_envelope(
            run_id=run_id, migration_id=migration_id,
            message_id=f"leader-activated-{role}", sender=leader_party,
            recipient=worker_party, nonce=nonce, leader_generation=2,
            leader_binding_id=new_leader["binding_id"],
            executor_parent_binding_id=new_leader["binding_id"],
            reviewer_parent_binding_id=new_leader["binding_id"],
            roles_sha256=activation_roles_digest,
        )
        request_hash = envelope_request_hash(request)
        delivery_key = operation_key(migration_id, f"activation-{role}", "deliverPrompt")
        transport.deliverPrompt(
            delivery_key, worker["session_id"], f"activation-{role}", nonce,
            request_hash, message_id=request["message_id"],
        )
        read_key = operation_key(migration_id, f"activation-read-{role}", "observeRoleState")
        state_readback = transport.observeRoleState(read_key, role)
        role_readback = next(item for item in state_readback["roles"] if item["role"] == role)
        if state_readback["leader_generation"] != 2 or role_readback["leader_generation"] != 2 or role_readback["parent_binding_id"] != new_leader["binding_id"]:
            raise ProtocolError("WORKER_ACTIVATION_READBACK_MISMATCH", role)
        ack_key = operation_key(migration_id, f"activation-ack-{role}", "reportToLeader")
        receipt = _fake_activation_receipt(
            request=request, sender=worker_party, recipient=leader_party,
            operation_key_value=ack_key,
        )
        ack_report = {
            "kind": "ACTIVATION_ACK", "operation_key": ack_key, "role": role,
            "binding_id": worker["binding_id"], "session_id": worker["session_id"],
            "leader_generation": 2, "parent_binding_id": new_leader["binding_id"],
            "activation_message_id": request["message_id"],
            "activation_sha256": request_hash, "roles_sha256": activation_roles_digest,
            "role_state_sha256": digest(state_readback), "outcome": "SUCCEEDED",
        }
        transport.reportToLeader(ack_key, ack_report)
        envelopes.extend((request, receipt))
        probe_results.append({
            "probe_id": f"activation-ack-{role}", "owner": role,
            "passed": True, "evidence": ack_report,
        })
    probe_results.extend({
        "probe_id": record["probe_id"], "owner": record["owner"],
        "passed": record["outcome"] == "SUCCEEDED", "evidence": record,
    } for record in gate_observed["probe_records"])
    workers_acked_key = operation_key(migration_id, "workers-acked", "reportToLeader")
    paired("WORKERS_ACKED", workers_acked_key, lambda: transport.reportToLeader(workers_acked_key, {"operation_key": workers_acked_key, "workers": ["executor", "reviewer"], "outcome": "SUCCEEDED"}))
    commit_key = operation_key(migration_id, "commit-readback", "reconcile")
    supersede_key = operation_key(migration_id, "supersede-old", "bindRole")
    stop_key = operation_key(migration_id, "stop-old", "stopSession")

    def final_graph_valid(value: Mapping[str, Any]) -> bool:
        bindings = value.get("bindings")
        sessions = value.get("sessions")
        if not isinstance(bindings, Mapping) or not isinstance(sessions, Mapping):
            return False
        old_value = bindings.get(old_leader["binding_id"])
        new_value = bindings.get(new_leader["binding_id"])
        return bool(
            value.get("final_reconciled") is True
            and isinstance(old_value, Mapping)
            and old_value.get("authority_state") == "FROZEN"
            and isinstance(new_value, Mapping)
            and new_value.get("authority_state") == "ACTIVE"
            and sessions.get(old_leader["session_id"], {}).get("running") is True
            and all(
                isinstance(bindings.get(worker["binding_id"]), Mapping)
                and bindings[worker["binding_id"]].get("leader_generation") == 2
                and bindings[worker["binding_id"]].get("parent_binding_id") == new_leader["binding_id"]
                for worker in after["roles"][1:]
            )
        )

    def final_reconcile_effect() -> Mapping[str, Any]:
        return coordinator.protected_mutation(
            "final_reconcile",
            lambda: transport.reconcile(key=commit_key, final=True),
            reconcile_around=False,
        )

    final_graph = coordinator.journaled_effect(
        phase="OLD_SUPERSEDED", key=commit_key, desired={"final": True},
        readback_predicate="complete_generation_graph_with_old_fenced",
        effect=final_reconcile_effect, readback=transport.reconciliation_readback,
        validate_readback=final_graph_valid, timestamp=next_time,
        advance_phase=False,
    )
    reconciled = journal_helper.load()
    reconciled["observed_binding_parents"] = {
        worker["binding_id"]: final_graph["bindings"][worker["binding_id"]]["parent_binding_id"]
        for worker in after["roles"][1:]
    }
    if reconciled["observed_binding_parents"] != reconciled["expected_binding_parents"]:
        raise ProtocolError("FINAL_RECONCILE_MISMATCH", "worker parents")
    journal_helper.write(reconciled)

    supersede_desired = {
        "role": "leader", "binding_id": old_leader["binding_id"],
        "parent_binding_id": None, "leader_generation": 1,
        "authority_state": "SUPERSEDED",
    }
    coordinator.journaled_effect(
        phase="OLD_SUPERSEDED", key=supersede_key, desired=supersede_desired,
        readback_predicate="old_binding_exactly_superseded",
        effect=lambda: coordinator.protected_mutation(
            "supersede_old", lambda: transport.bindRole(supersede_key, supersede_desired),
            reconcile_around=False,
        ),
        readback=lambda: copy.deepcopy(transport.bindings[old_leader["binding_id"]]),
        validate_readback=lambda value: dict(value) == supersede_desired,
        timestamp=next_time,
    )

    stop_desired = {"session_id": old_leader["session_id"], "running": False}

    def stop_old_effect() -> Mapping[str, Any]:
        return coordinator.protected_mutation(
            "stop_old", lambda: transport.stopSession(stop_key, old_leader["session_id"]),
            reconcile_around=False,
        )

    def stop_old_readback() -> Mapping[str, Any]:
        value = {
            "session_id": old_leader["session_id"],
            "running": bool(transport.sessions[old_leader["session_id"]]["running"]),
        }
        if value["running"] is False:
            # Local durable-state persistence may follow the final CC-Panes
            # effect.  Recovery performs the same idempotent snapshot update
            # when stopSession completed before the coordinator crashed.
            transport.role_state = copy.deepcopy(after)
            transport.role_state_readback = copy.deepcopy(after)
            transport.assert_invariants()
        return value

    coordinator.journaled_effect(
        phase="COMMITTED", key=stop_key, desired=stop_desired,
        readback_predicate="old_session_exactly_stopped",
        effect=stop_old_effect,
        readback=stop_old_readback,
        validate_readback=lambda value: dict(value) == stop_desired,
        timestamp=next_time,
    )
    journal = journal_helper.load()
    scratch.cleanup()
    return build_e2e_evidence(
        run_id=run_id, transport=transport, compatibility_artifact=artifact,
        compatibility_row_id=row_id, before_role_state=before, after_role_state=after,
        before_bindings=before_bindings, after_bindings=after_bindings,
        migration_journal=journal, candidate_ledger_records=candidate_records,
        gate_observed=gate_observed, envelopes=list(envelopes), probe_results=probe_results,
        assertions=[
            {"name": "exactly-three-panes", "passed": True, "evidence": sorted({role["pane_id"] for role in after["roles"]})},
            {"name": "one-cooperative-authority", "passed": True, "evidence": {"binding_id": new_leader["binding_id"], "generation": 2}},
            {"name": "recoverable-parent-graph", "passed": True, "evidence": after_bindings},
            {"name": "durable-role-state-readback", "passed": transport.role_state_readback == after, "evidence": transport.role_state_readback},
            {"name": "workers-activation-acked", "passed": set(transport.worker_acks) == {"executor", "reviewer"}, "evidence": transport.worker_acks},
            {"name": "old-stop-after-final-reconcile", "passed": transport.final_reconciled and not transport.sessions[old_leader["session_id"]]["running"], "evidence": {"final_reconciled": transport.final_reconciled, "old_session": transport.sessions[old_leader["session_id"]]}},
            {"name": "continuous-authority-oracle", "passed": bool(transport.oracle_trace), "evidence": transport.oracle_trace},
        ],
        command_records=[{"command": "protocol_harness.py fake-e2e", "exit_code": 0, "stdout": "deterministic fake smoke passed", "stderr": "", "provenance": {"kind": "python-standard-library", "project_root": str(root)}}],
        timestamps=["2026-07-26T12:00:00Z", "2026-07-26T13:00:00Z"], outcome="COMMITTED",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fake = subparsers.add_parser("fake-e2e", help="run supported deterministic fake smoke")
    fake.add_argument("--root", type=Path, required=True)
    fake.add_argument("--run-id", required=True)
    fake.add_argument("--output", type=Path, required=True)
    fake.add_argument("--compatibility", type=Path, default=Path(__file__).resolve().parents[1] / "compatibility.json")
    live = subparsers.add_parser(
        "validate-live-smoke", help="validate a captured pre-authority live smoke",
    )
    live.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-live-smoke":
            path = args.path.resolve()
            value = validate_pre_authority_live_smoke(
                json.loads(path.read_text(encoding="utf-8"))
            )
            print(json.dumps({
                "path": str(path), "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                "run_id": value["run_id"], "status": value["status"],
            }, sort_keys=True))
        else:
            root = args.root.resolve()
            output = args.output.resolve()
            allowed = (root / ".codex/e2e").resolve()
            output.relative_to(allowed)
            evidence = run_supported_fake_e2e(root, args.run_id, args.compatibility.resolve())
            evidence_digest = write_e2e_evidence(output, evidence)
            print(json.dumps({"output": str(output), "digest": evidence_digest, "run_id": args.run_id}, sort_keys=True))
    except (OSError, KeyError, ValueError, ProtocolError) as exc:
        print(f"protocol_harness: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "agent-team-workflow" / "scripts" / "workflow_guard.py"
SPEC = importlib.util.spec_from_file_location("workflow_guard", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("workflow_guard import failed")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class WorkflowGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def init(self, run_id: str = "run-1") -> dict:
        return guard.init_guard(self.root, run_id)

    def test_init_and_status_use_exact_schema_one_defaults(self) -> None:
        first = self.init()
        second = self.init()
        self.assertEqual(first, second)
        self.assertEqual(1, first["schema_version"])
        self.assertEqual("run-1", first["run_id"])
        self.assertEqual("lean", first["mode"])
        self.assertEqual(guard.DEFAULT_LIMITS, first["limits"])
        self.assertEqual([], first["events"])
        status = guard.status_guard(self.root, "run-1")
        self.assertTrue(status["ok"])
        self.assertEqual(0, status["event_count"])

    def test_every_limit_returns_its_stable_code_and_persists_evidence(self) -> None:
        cases = (
            ("spec_revision", 1, "SPEC_LIMIT_REACHED"),
            ("internal_agent", 0, "AGENT_LIMIT_REACHED"),
            ("implementation_review", 1, "REVIEW_LIMIT_REACHED"),
            ("focused_re_review", 1, "REVIEW_LIMIT_REACHED"),
            ("full_test_suite", 1, "TEST_LIMIT_REACHED"),
            ("scope_expansion", 0, "SCOPE_APPROVAL_REQUIRED"),
        )
        for index, (action, allowance, code) in enumerate(cases):
            run_id = f"limit-{index}"
            self.init(run_id)
            for attempt in range(allowance):
                result = guard.consume_guard(
                    self.root,
                    run_id,
                    action=action,
                    operation_key=f"{action}-{attempt}",
                )
                self.assertTrue(result["ok"])
            with self.assertRaises(guard.GuardDenied) as captured:
                guard.consume_guard(
                    self.root,
                    run_id,
                    action=action,
                    operation_key=f"{action}-denied",
                )
            self.assertEqual(code, captured.exception.code)
            state = guard.status_guard(self.root, run_id)["state"]
            self.assertEqual("DENIED", state["events"][-1]["outcome"])
            self.assertEqual(code, state["events"][-1]["code"])

    def test_operation_key_replay_is_idempotent_for_success_and_denial(self) -> None:
        self.init()
        first = guard.consume_guard(
            self.root,
            "run-1",
            action="implementation_review",
            operation_key="review-once",
        )
        replay = guard.consume_guard(
            self.root,
            "run-1",
            action="implementation_review",
            operation_key="review-once",
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(guard.GuardDenied):
            guard.consume_guard(
                self.root,
                "run-1",
                action="implementation_review",
                operation_key="review-denied",
            )
        with self.assertRaises(guard.GuardDenied) as captured:
            guard.consume_guard(
                self.root,
                "run-1",
                action="implementation_review",
                operation_key="review-denied",
            )
        self.assertTrue(captured.exception.evidence["replayed"])
        self.assertEqual(2, guard.status_guard(self.root, "run-1")["event_count"])

    def test_retry_allowance_is_independent_per_required_failure_key(self) -> None:
        self.init()
        for failure_key in ("failure-a", "failure-b"):
            for attempt in range(2):
                guard.consume_guard(
                    self.root,
                    "run-1",
                    action="same_failure_retry",
                    operation_key=f"{failure_key}-{attempt}",
                    failure_key=failure_key,
                )
        with self.assertRaises(guard.GuardDenied) as captured:
            guard.consume_guard(
                self.root,
                "run-1",
                action="same_failure_retry",
                operation_key="failure-a-denied",
                failure_key="failure-a",
            )
        self.assertEqual("RETRY_LIMIT_REACHED", captured.exception.code)
        status = guard.status_guard(self.root, "run-1")
        self.assertEqual({"failure-a": 2, "failure-b": 2}, status["retry_usage"])
        with self.assertRaisesRegex(guard.GuardError, "requires failure_key"):
            guard.consume_guard(
                self.root,
                "run-1",
                action="same_failure_retry",
                operation_key="missing-failure",
            )

    def test_operation_key_conflicts_fail_closed_without_new_event(self) -> None:
        self.init()
        guard.consume_guard(
            self.root,
            "run-1",
            action="spec_revision",
            operation_key="shared-key",
        )
        with self.assertRaises(guard.GuardError) as captured:
            guard.consume_guard(
                self.root,
                "run-1",
                action="implementation_review",
                operation_key="shared-key",
            )
        self.assertEqual("OPERATION_KEY_CONFLICT", captured.exception.code)
        self.assertEqual(1, guard.status_guard(self.root, "run-1")["event_count"])

    def test_malformed_state_and_run_mismatch_fail_closed(self) -> None:
        self.init()
        path = guard.guard_path(self.root, "run-1")
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(guard.GuardError) as malformed:
            guard.status_guard(self.root, "run-1")
        self.assertEqual("INVALID_STATE", malformed.exception.code)

        state = {
            "schema_version": 1,
            "run_id": "different-run",
            "mode": "lean",
            "limits": dict(guard.DEFAULT_LIMITS),
            "events": [],
        }
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(guard.GuardError) as mismatch:
            guard.status_guard(self.root, "run-1")
        self.assertEqual("RUN_ID_MISMATCH", mismatch.exception.code)

    def test_concurrent_consumers_serialize_to_one_allowance(self) -> None:
        self.init()

        def consume(index: int) -> str:
            try:
                guard.consume_guard(
                    self.root,
                    "run-1",
                    action="implementation_review",
                    operation_key=f"review-{index}",
                )
                return "CONSUMED"
            except guard.GuardDenied:
                return "DENIED"

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(consume, range(8)))
        self.assertEqual(1, outcomes.count("CONSUMED"))
        self.assertEqual(7, outcomes.count("DENIED"))
        status = guard.status_guard(self.root, "run-1")
        self.assertEqual(8, status["event_count"])
        self.assertEqual(1, status["usage"]["implementation_reviews"])
        guard.validate_state(status["state"], expected_run_id="run-1")
        self.assertFalse(guard.guard_path(self.root, "run-1").with_suffix(".json.lock").exists())

    def test_cli_init_consume_status_and_denial_exit_codes(self) -> None:
        common = [sys.executable, str(SCRIPT), "--root", str(self.root), "--run-id", "cli-run"]
        initialized = subprocess.run(
            [sys.executable, str(SCRIPT), "init", "--root", str(self.root), "--run-id", "cli-run"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, initialized.returncode)
        consumed = subprocess.run(
            [sys.executable, str(SCRIPT), "consume", "--root", str(self.root), "--run-id", "cli-run", "--action", "implementation_review", "--operation-key", "cli-review"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, consumed.returncode)
        denied = subprocess.run(
            [sys.executable, str(SCRIPT), "consume", "--root", str(self.root), "--run-id", "cli-run", "--action", "implementation_review", "--operation-key", "cli-review-denied"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual("REVIEW_LIMIT_REACHED", json.loads(denied.stdout)["code"])
        status = subprocess.run(
            [sys.executable, str(SCRIPT), "status", "--root", str(self.root), "--run-id", "cli-run"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, status.returncode)
        self.assertEqual(2, json.loads(status.stdout)["event_count"])


if __name__ == "__main__":
    unittest.main()

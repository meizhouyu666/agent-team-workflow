#!/usr/bin/env python3
"""Deterministic run-scoped guardrails for protocol-compliant lean workflows."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
MODE = "lean"
DEFAULT_LIMITS = {
    "spec_revisions": 1,
    "internal_agents": 0,
    "implementation_reviews": 1,
    "focused_re_reviews": 1,
    "same_failure_retries": 2,
    "full_test_suite_runs": 1,
    "scope_expansions": 0,
}
ACTION_RULES = {
    "spec_revision": ("spec_revisions", "SPEC_LIMIT_REACHED"),
    "internal_agent": ("internal_agents", "AGENT_LIMIT_REACHED"),
    "implementation_review": ("implementation_reviews", "REVIEW_LIMIT_REACHED"),
    "focused_re_review": ("focused_re_reviews", "REVIEW_LIMIT_REACHED"),
    "same_failure_retry": ("same_failure_retries", "RETRY_LIMIT_REACHED"),
    "full_test_suite": ("full_test_suite_runs", "TEST_LIMIT_REACHED"),
    "scope_expansion": ("scope_expansions", "SCOPE_APPROVAL_REQUIRED"),
}
STATE_FIELDS = {"schema_version", "run_id", "mode", "limits", "events"}
EVENT_FIELDS = {
    "sequence",
    "action",
    "operation_key",
    "failure_key",
    "outcome",
    "code",
}
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOCK_TIMEOUT_SECONDS = 5.0


class GuardError(Exception):
    """Fail-closed validation or identity error."""

    def __init__(self, code: str, message: str, *, evidence: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence

    def result(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.evidence is not None:
            value["evidence"] = self.evidence
        return value


class GuardDenied(GuardError):
    """A valid protected action exceeded its configured allowance."""


def _require_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise GuardError("INVALID_RUN_ID", "run_id must be a safe non-empty identifier")
    return run_id


def guard_path(root: Path | str, run_id: str) -> Path:
    validated = _require_run_id(run_id)
    return Path(root).resolve() / ".codex" / "guards" / f"{validated}.json"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise GuardError("LOCK_UNAVAILABLE", f"guard lock is busy: {lock_path}")
            time.sleep(0.01)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _event_identity(event: Mapping[str, Any]) -> tuple[str, str | None]:
    return str(event["action"]), event["failure_key"]


def validate_state(state: Any, *, expected_run_id: str | None = None) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise GuardError("INVALID_STATE", "guard state has unexpected fields")
    if state["schema_version"] != SCHEMA_VERSION:
        raise GuardError("INVALID_STATE", "unsupported guard schema_version")
    run_id = _require_run_id(state["run_id"])
    if expected_run_id is not None and run_id != expected_run_id:
        raise GuardError("RUN_ID_MISMATCH", f"expected {expected_run_id}, found {run_id}")
    if state["mode"] != MODE:
        raise GuardError("MODE_MISMATCH", "guard mode must remain lean")
    if state["limits"] != DEFAULT_LIMITS:
        raise GuardError("INVALID_STATE", "lean limits do not match schema-1 defaults")
    if not isinstance(state["events"], list):
        raise GuardError("INVALID_STATE", "events must be an array")

    counts: dict[tuple[str, str | None], int] = {}
    operation_keys: set[str] = set()
    for expected_sequence, event in enumerate(state["events"], start=1):
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise GuardError("INVALID_STATE", "guard event has unexpected fields")
        if event["sequence"] != expected_sequence:
            raise GuardError("INVALID_STATE", "guard event sequence is not contiguous")
        action = event["action"]
        if action not in ACTION_RULES:
            raise GuardError("INVALID_STATE", f"unknown action: {action}")
        operation_key = event["operation_key"]
        if not isinstance(operation_key, str) or not operation_key:
            raise GuardError("INVALID_STATE", "operation_key must be non-empty")
        if operation_key in operation_keys:
            raise GuardError("INVALID_STATE", "operation_key must be unique")
        operation_keys.add(operation_key)

        failure_key = event["failure_key"]
        if action == "same_failure_retry":
            if not isinstance(failure_key, str) or not failure_key:
                raise GuardError("INVALID_STATE", "retry events require failure_key")
        elif failure_key is not None:
            raise GuardError("INVALID_STATE", "failure_key is retry-only")

        identity = _event_identity(event)
        consumed = counts.get(identity, 0)
        limit_name, denial_code = ACTION_RULES[action]
        limit = state["limits"][limit_name]
        if event["outcome"] == "CONSUMED":
            if event["code"] is not None or consumed >= limit:
                raise GuardError("INVALID_STATE", "consumed event exceeds its allowance")
            counts[identity] = consumed + 1
        elif event["outcome"] == "DENIED":
            if event["code"] != denial_code or consumed < limit:
                raise GuardError("INVALID_STATE", "denial evidence is inconsistent")
        else:
            raise GuardError("INVALID_STATE", "event outcome must be CONSUMED or DENIED")
    return state


def _read_state(path: Path, run_id: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise GuardError("STATE_NOT_INITIALIZED", f"guard state is missing: {path}") from exc
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("INVALID_STATE", "guard state is not valid UTF-8 JSON") from exc
    return validate_state(state, expected_run_id=run_id)


def init_guard(root: Path | str, run_id: str, *, mode: str = MODE) -> dict[str, Any]:
    if mode != MODE:
        raise GuardError("MODE_MISMATCH", "only lean guard state is supported")
    path = guard_path(root, run_id)
    with _exclusive_lock(path):
        if path.exists():
            return _read_state(path, run_id)
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mode": MODE,
            "limits": dict(DEFAULT_LIMITS),
            "events": [],
        }
        validate_state(state, expected_run_id=run_id)
        _atomic_write(path, state)
        return state


def _usage(state: Mapping[str, Any]) -> dict[str, Any]:
    consumed = [event for event in state["events"] if event["outcome"] == "CONSUMED"]
    usage = {
        limit_name: sum(
            1
            for event in consumed
            if ACTION_RULES[event["action"]][0] == limit_name
            and event["action"] != "same_failure_retry"
        )
        for limit_name in DEFAULT_LIMITS
        if limit_name != "same_failure_retries"
    }
    retry_usage: dict[str, int] = {}
    for event in consumed:
        if event["action"] == "same_failure_retry":
            failure_key = str(event["failure_key"])
            retry_usage[failure_key] = retry_usage.get(failure_key, 0) + 1
    return {
        "usage": usage,
        "retry_usage": dict(sorted(retry_usage.items())),
        "event_count": len(state["events"]),
    }


def status_guard(root: Path | str, run_id: str) -> dict[str, Any]:
    path = guard_path(root, run_id)
    with _exclusive_lock(path):
        state = _read_state(path, run_id)
        return {"ok": True, "state": state, **_usage(state)}


def consume_guard(
    root: Path | str,
    run_id: str,
    *,
    action: str,
    operation_key: str,
    failure_key: str | None = None,
) -> dict[str, Any]:
    if action not in ACTION_RULES:
        raise GuardError("UNKNOWN_ACTION", f"unknown guarded action: {action}")
    if not isinstance(operation_key, str) or not operation_key:
        raise GuardError("INVALID_OPERATION_KEY", "operation_key must be non-empty")
    if action == "same_failure_retry":
        if not isinstance(failure_key, str) or not failure_key:
            raise GuardError("FAILURE_KEY_REQUIRED", "same_failure_retry requires failure_key")
    elif failure_key is not None:
        raise GuardError("UNEXPECTED_FAILURE_KEY", "failure_key is retry-only")

    path = guard_path(root, run_id)
    with _exclusive_lock(path):
        state = _read_state(path, run_id)
        for event in state["events"]:
            if event["operation_key"] != operation_key:
                continue
            if _event_identity(event) != (action, failure_key):
                raise GuardError(
                    "OPERATION_KEY_CONFLICT",
                    "operation_key was already used for a different guarded action",
                )
            result = {"event": event, "replayed": True, **_usage(state)}
            if event["outcome"] == "DENIED":
                raise GuardDenied(event["code"], "guarded action remains denied", evidence=result)
            return {"ok": True, **result}

        identity = (action, failure_key)
        consumed = sum(
            1
            for event in state["events"]
            if event["outcome"] == "CONSUMED" and _event_identity(event) == identity
        )
        limit_name, denial_code = ACTION_RULES[action]
        limit = state["limits"][limit_name]
        allowed = consumed < limit
        event = {
            "sequence": len(state["events"]) + 1,
            "action": action,
            "operation_key": operation_key,
            "failure_key": failure_key,
            "outcome": "CONSUMED" if allowed else "DENIED",
            "code": None if allowed else denial_code,
        }
        state["events"].append(event)
        validate_state(state, expected_run_id=run_id)
        _atomic_write(path, state)
        result = {"event": event, "replayed": False, **_usage(state)}
        if not allowed:
            raise GuardDenied(denial_code, "guarded action limit reached", evidence=result)
        return {"ok": True, **result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status"):
        command = subcommands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--run-id", required=True)
    init = subcommands.choices["init"]
    init.add_argument("--mode", default=MODE)

    consume = subcommands.add_parser("consume")
    consume.add_argument("--root", type=Path, default=Path.cwd())
    consume.add_argument("--run-id", required=True)
    consume.add_argument("--action", required=True, choices=sorted(ACTION_RULES))
    consume.add_argument("--operation-key", required=True)
    consume.add_argument("--failure-key")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            state = init_guard(args.root, args.run_id, mode=args.mode)
            result = {"ok": True, "state": state, **_usage(state)}
        elif args.command == "status":
            result = status_guard(args.root, args.run_id)
        else:
            result = consume_guard(
                args.root,
                args.run_id,
                action=args.action,
                operation_key=args.operation_key,
                failure_key=args.failure_key,
            )
    except GuardError as exc:
        print(json.dumps(exc.result(), ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

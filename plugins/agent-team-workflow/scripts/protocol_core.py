#!/usr/bin/env python3
"""Deterministic protocol and persistence core for agent-team-workflow 0.2.

The module deliberately depends only on the Python standard library.  Transport
adapters supply observations and effects; this module validates, fences, records,
and reconciles them without attempting to be a daemon.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence


WORKFLOW_VERSION = "0.2.0"
ROLE_SCHEMA_VERSION = MIGRATION_SCHEMA_VERSION = ENVELOPE_SCHEMA_VERSION = 1
ROLES = ("leader", "executor", "reviewer")
AUTHORITY_STATES = ("ACTIVE", "CANDIDATE", "FROZEN", "SUPERSEDED", "QUARANTINED")
MESSAGE_TYPES = (
    "HANDSHAKE", "ASSIGNMENT", "RECEIPT_ACK", "COMPLETION",
    "REVIEW_REQUEST", "REVIEW_VERDICT", "LEADER_ACTIVATED",
)
CANDIDATE_TYPES = ("HANDSHAKE", "RECEIPT_ACK", "COMPLETION")
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "INDETERMINATE")
PROCESSING_STATES = ("RECEIVED", "EXECUTING", *TERMINAL_STATES)
MIGRATION_PHASES = (
    "CREATED", "PREFLIGHTED", "PREPARED", "VALIDATED",
    "NEW_LEADER_REGISTERED", "EXECUTOR_REPARENTED", "REVIEWER_REPARENTED",
    "STATE_ACTIVATED", "WORKERS_ACKED", "OLD_SUPERSEDED", "COMMITTED",
    "ABORTED", "ROLLED_BACK",
)
PHASE_ORDER = {value: index for index, value in enumerate(MIGRATION_PHASES[:11])}
COMMIT_PHASES = MIGRATION_PHASES[1:11]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")


class ProtocolError(ValueError):
    """Fail-closed protocol error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class EffectPolicy(str, Enum):
    PURE = "PURE"
    RECONCILABLE_IDEMPOTENT = "RECONCILABLE_IDEMPOTENT"
    UNRECONCILABLE = "UNRECONCILABLE"


class Observation(str, Enum):
    COMPLETE = "COMPLETE"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


def _fail(code: str, message: str) -> None:
    raise ProtocolError(code, message)


def _exact(obj: Mapping[str, Any], required: Iterable[str], optional: Iterable[str] = ()) -> None:
    required_set, optional_set = set(required), set(optional)
    missing = required_set - set(obj)
    extra = set(obj) - required_set - optional_set
    if missing:
        _fail("MISSING_FIELD", ", ".join(sorted(missing)))
    if extra:
        _fail("EXTRA_FIELD", ", ".join(sorted(extra)))


def _string(value: Any, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value:
        _fail("INVALID_FIELD", f"{name} must be a non-empty string")


def normalize_timestamp(value: str) -> str:
    """Return a UTC RFC3339 timestamp with deterministic millisecond-free form."""
    _string(value, "timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("INVALID_TIMESTAMP", value)
    if parsed.tzinfo is None:
        _fail("INVALID_TIMESTAMP", "timezone is required")
    parsed = parsed.astimezone(dt.timezone.utc)
    text = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    if parsed.microsecond:
        text += "." + f"{parsed.microsecond:06d}".rstrip("0")
    return text + "Z"


def _parse_timestamp(value: str) -> dt.datetime:
    normalize_timestamp(value)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _normalize_for_canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_for_canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_for_canonical(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize_for_canonical(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("INVALID_JSON", f"{path}: {exc}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def exclusive_lock(target: Path) -> Iterator[None]:
    """Acquire a crash-released cooperative, non-waiting sibling lock."""
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.path.getsize(lock) == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                _fail("LOCKED", str(target))
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _fail("LOCKED", str(target))
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if backup and path.exists():
            backup_path = path.with_name(path.name + ".validated.bak")
            shutil.copyfile(path, backup_path)
            with backup_path.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


ROLE_FIELDS = (
    "schema_version", "role", "cli_tool", "adapter_id", "runtime_kind",
    "project_root", "session_id", "resume_id",
    "binding_id", "parent_binding_id", "leader_generation", "authority_state",
    "required_capabilities", "advertised_capabilities", "verified_at",
)


def validate_role_descriptor(role: Mapping[str, Any], expected_role: str | None = None) -> None:
    if not isinstance(role, dict):
        _fail("INVALID_ROLE", "role descriptor must be an object")
    _exact(role, ROLE_FIELDS)
    if role["schema_version"] != 1:
        _fail("UNSUPPORTED_SCHEMA", "role descriptor schema must be 1")
    if role["role"] not in ROLES or (expected_role and role["role"] != expected_role):
        _fail("INVALID_ROLE", str(role["role"]))
    for key in ("cli_tool", "adapter_id", "runtime_kind", "project_root", "session_id", "binding_id"):
        _string(role[key], key)
    _string(role["resume_id"], "resume_id", nullable=True)
    _string(role["parent_binding_id"], "parent_binding_id", nullable=True)
    if not isinstance(role["leader_generation"], int) or role["leader_generation"] < 0:
        _fail("INVALID_GENERATION", str(role["leader_generation"]))
    if role["authority_state"] not in AUTHORITY_STATES:
        _fail("INVALID_AUTHORITY", str(role["authority_state"]))
    for key in ("required_capabilities", "advertised_capabilities"):
        if not isinstance(role[key], list) or any(not isinstance(x, str) or not x for x in role[key]):
            _fail("INVALID_CAPABILITIES", key)
        if role[key] != sorted(set(role[key])):
            _fail("NON_CANONICAL_CAPABILITIES", key)
    if not set(role["required_capabilities"]).issubset(role["advertised_capabilities"]):
        _fail("MISSING_CAPABILITY", role["role"])
    normalize_timestamp(role["verified_at"])


def validate_role_state(state: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        _fail("INVALID_ROLE_STATE", "document must be an object")
    _exact(state, (
        "schema_version", "workflow_version", "run_id", "leader_generation",
        "migration_id", "migration_phase", "roles",
    ))
    if state["schema_version"] != 1:
        _fail("UNSUPPORTED_SCHEMA", "role state schema must be 1")
    if state["workflow_version"] != WORKFLOW_VERSION:
        _fail("UNSUPPORTED_WORKFLOW", str(state["workflow_version"]))
    _string(state["run_id"], "run_id")
    if not isinstance(state["leader_generation"], int) or state["leader_generation"] < 0:
        _fail("INVALID_GENERATION", str(state["leader_generation"]))
    _string(state["migration_id"], "migration_id", nullable=True)
    if state["migration_phase"] is not None and state["migration_phase"] not in MIGRATION_PHASES:
        _fail("INVALID_PHASE", str(state["migration_phase"]))
    if (state["migration_id"] is None) != (state["migration_phase"] is None):
        _fail("MIGRATION_STATE_MISMATCH", "migration ID and phase must both be null or present")
    roles = state["roles"]
    if not isinstance(roles, list) or len(roles) != 3:
        _fail("INVALID_ROLE_COUNT", "exactly three roles are required")
    if [item.get("role") if isinstance(item, dict) else None for item in roles] != list(ROLES):
        _fail("INVALID_ROLE_ORDER", "roles must be leader, executor, reviewer")
    bindings: set[str] = set()
    sessions: set[str] = set()
    active_leaders = 0
    for role_name, role in zip(ROLES, roles):
        validate_role_descriptor(role, role_name)
        if role["leader_generation"] != state["leader_generation"]:
            _fail("GENERATION_MISMATCH", role_name)
        if role["binding_id"] in bindings:
            _fail("DUPLICATE_BINDING", role["binding_id"])
        bindings.add(role["binding_id"])
        if role["session_id"] in sessions:
            _fail("DUPLICATE_SESSION", role["session_id"])
        sessions.add(role["session_id"])
        if role_name == "leader":
            if role["parent_binding_id"] is not None:
                _fail("INVALID_PARENT", "leader parent must be null")
            active_leaders += role["authority_state"] == "ACTIVE"
        elif role["parent_binding_id"] != roles[0]["binding_id"]:
            _fail("INVALID_PARENT", role_name)
    if active_leaders != 1:
        _fail("INVALID_AUTHORITY", "exactly one active leader is required")
    return copy.deepcopy(state)


def write_role_state(path: Path, state: Mapping[str, Any], *, expected_generation: int | None = None) -> str:
    validated = validate_role_state(state)
    with exclusive_lock(path):
        if path.exists():
            current = validate_role_state(load_json(path))
            if expected_generation is not None and current["leader_generation"] != expected_generation:
                _fail("STALE_WRITER", "role-state generation changed")
            if validated["leader_generation"] < current["leader_generation"]:
                _fail("GENERATION_REGRESSION", "leader generation cannot decrease")
        raw = canonical_bytes(validated) + b"\n"
        atomic_write(path, raw, backup=True)
    return hashlib.sha256(raw).hexdigest()


def recover_role_state(path: Path, reconcile: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Load canonical state or a validated backup accepted by live reconciliation."""
    try:
        state = validate_role_state(load_json(path))
    except ProtocolError:
        corrupt = path.read_bytes() if path.exists() else b""
        backup = path.with_name(path.name + ".validated.bak")
        if not backup.exists():
            raise
        state = validate_role_state(load_json(backup))
        if not reconcile(state):
            _fail("RECONCILIATION_FAILED", "backup does not match the live graph")
        if corrupt:
            atomic_write(path.with_name(path.name + ".corrupt." + digest(corrupt) + ".quarantine"), corrupt)
        atomic_write(path, canonical_bytes(state) + b"\n")
    return state


def parse_legacy_scalars(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-z][a-z0-9_-]*):\s*(.*?)\s*$", line)
        if match:
            if match.group(1) in result:
                _fail("INVALID_LEGACY", f"duplicate {match.group(1)}")
            result[match.group(1)] = match.group(2)
    return result


def upgrade_schema0(path: Path, legacy_path: Path, proposed: Mapping[str, Any], live_roles: Sequence[Mapping[str, Any]]) -> str:
    """Perform the sole one-time schema-0 upgrade after exact live validation."""
    if path.exists():
        _fail("UPGRADE_ALREADY_ATTEMPTED", str(path))
    if not legacy_path.is_file():
        _fail("NO_VALID_LEGACY", str(legacy_path))
    scalars = parse_legacy_scalars(legacy_path)
    if not scalars or "run_id" not in scalars:
        _fail("INVALID_LEGACY", "run_id scalar is required")
    state = validate_role_state(proposed)
    if state["run_id"] != scalars["run_id"]:
        _fail("LEGACY_MISMATCH", "run_id")
    if len(live_roles) != 3:
        _fail("LIVE_VALIDATION_FAILED", "exactly three live roles required")
    for expected, observed in zip(state["roles"], live_roles):
        keys = ("role", "cli_tool", "session_id", "binding_id", "parent_binding_id", "project_root")
        if expected["cli_tool"] != "codex" or any(observed.get(key) != expected[key] for key in keys):
            _fail("LIVE_VALIDATION_FAILED", expected["role"])
    return write_role_state(path, state)


PARTY_FIELDS = ("role", "cli_tool", "session_id", "binding_id")
ENVELOPE_FIELDS = (
    "schema_version", "authority_scope", "message_id", "reply_to", "run_id",
    "migration_id", "leader_generation", "message_type", "sender", "recipient",
    "nonce", "created_at", "expires_at", "payload", "payload_path",
    "payload_sha256",
)


def _validate_party(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        _fail("INVALID_PARTY", name)
    _exact(value, PARTY_FIELDS)
    if value["role"] not in ROLES:
        _fail("INVALID_ROLE", name)
    for key in PARTY_FIELDS[1:]:
        _string(value[key], f"{name}.{key}")


def _is_reparse_or_symlink(path: Path) -> bool:
    """Inspect one lexical path component without following its final link."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail("PAYLOAD_PATH_UNAVAILABLE", f"{path}: {exc}")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _payload_lexical_path(project_root: Path, relative: str) -> tuple[Path, Path, tuple[str, ...]]:
    """Return an absolute lexical payload path without resolving any component."""
    supplied = Path(relative)
    if supplied.is_absolute():
        _fail("PATH_TRAVERSAL", relative)
    root = Path(os.path.abspath(project_root))
    base = root / ".codex" / "messages" / "payloads"
    candidate = Path(os.path.abspath(root / supplied))
    try:
        common = Path(os.path.commonpath((str(base), str(candidate))))
    except ValueError:
        _fail("PATH_TRAVERSAL", relative)
    if os.path.normcase(str(common)) != os.path.normcase(str(base)) or candidate == base:
        _fail("PATH_TRAVERSAL", relative)
    try:
        remainder = candidate.relative_to(base).parts
    except ValueError:
        _fail("PATH_TRAVERSAL", relative)
    if not remainder or any(part in ("", ".", "..") for part in remainder):
        _fail("PATH_TRAVERSAL", relative)
    return root, candidate, remainder


def _inspect_payload_components(root: Path, candidate: Path) -> None:
    """Reject links/reparse points in the registered root and every child component."""
    if _is_reparse_or_symlink(root):
        _fail("SYMLINK_REJECTED", str(root))
    relative_parts = candidate.relative_to(root).parts
    cursor = root
    for part in relative_parts:
        cursor = cursor / part
        if _is_reparse_or_symlink(cursor):
            _fail("SYMLINK_REJECTED", str(cursor))


def _read_payload_posix(root: Path, remainder: tuple[str, ...]) -> bytes:
    """Open every POSIX component relative to a pinned no-follow directory handle."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
    if nofollow is None or directory is None or not supports_dir_fd:
        _fail("SECURE_OPEN_UNAVAILABLE", "dir_fd/O_NOFOLLOW/O_DIRECTORY required")
    base = root / ".codex" / "messages" / "payloads"
    fd = os.open(str(base), os.O_RDONLY | directory | nofollow)
    try:
        for component in remainder[:-1]:
            child = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = child
        payload_fd = os.open(remainder[-1], os.O_RDONLY | nofollow, dir_fd=fd)
        try:
            metadata = os.fstat(payload_fd)
            if not stat.S_ISREG(metadata.st_mode):
                _fail("PAYLOAD_NOT_FILE", remainder[-1])
            chunks: list[bytes] = []
            while True:
                chunk = os.read(payload_fd, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(payload_fd)
    except OSError as exc:
        _fail("SECURE_PAYLOAD_OPEN_FAILED", str(exc))
    finally:
        os.close(fd)


def _read_payload_windows(candidate: Path) -> bytes:
    """Read a Windows payload through one non-reparse, replacement-denying handle."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        _fail("SECURE_OPEN_UNAVAILABLE", "Windows handle APIs unavailable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
    get_info.restype = wintypes.BOOL
    get_name = kernel32.GetFinalPathNameByHandleW
    get_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_name.restype = wintypes.DWORD
    read_file = kernel32.ReadFile
    read_file.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID)
    read_file.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)

    class _FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD), ("created", _FileTime),
            ("accessed", _FileTime), ("written", _FileTime),
            ("volume_serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD), ("file_index_low", wintypes.DWORD),
        )

    generic_read = 0x80000000
    share_read = 0x00000001  # Deliberately deny write/delete replacement while bound.
    open_existing = 3
    open_reparse = 0x00200000
    sequential = 0x08000000
    invalid_handle = wintypes.HANDLE(-1).value
    handle = create_file(str(candidate), generic_read, share_read, None, open_existing, open_reparse | sequential, None)
    if handle == invalid_handle:
        _fail("SECURE_PAYLOAD_OPEN_FAILED", f"{candidate}: winerror={ctypes.get_last_error()}")
    try:
        information = _ByHandleFileInformation()
        if not get_info(handle, ctypes.byref(information)):
            _fail("SECURE_PAYLOAD_OPEN_FAILED", f"file information: winerror={ctypes.get_last_error()}")
        if information.attributes & 0x400:
            _fail("SYMLINK_REJECTED", str(candidate))
        if information.attributes & 0x10:
            _fail("PAYLOAD_NOT_FILE", str(candidate))

        required = get_name(handle, None, 0, 0)
        if not required:
            _fail("SECURE_PAYLOAD_OPEN_FAILED", f"final path: winerror={ctypes.get_last_error()}")
        buffer = ctypes.create_unicode_buffer(required + 1)
        if not get_name(handle, buffer, len(buffer), 0):
            _fail("SECURE_PAYLOAD_OPEN_FAILED", f"final path: winerror={ctypes.get_last_error()}")
        final_name = buffer.value
        if final_name.startswith("\\\\?\\"):
            final_name = final_name[4:]
        if os.path.normcase(os.path.abspath(final_name)) != os.path.normcase(os.path.abspath(candidate)):
            _fail("SYMLINK_REJECTED", str(candidate))

        chunks: list[bytes] = []
        while True:
            chunk = ctypes.create_string_buffer(1024 * 1024)
            count = wintypes.DWORD()
            if not read_file(handle, chunk, len(chunk), ctypes.byref(count), None):
                _fail("SECURE_PAYLOAD_READ_FAILED", f"winerror={ctypes.get_last_error()}")
            if count.value == 0:
                return b"".join(chunks)
            chunks.append(chunk.raw[:count.value])
    finally:
        close_handle(handle)


def _secure_read_payload_bytes(project_root: Path, relative: str) -> bytes:
    root, candidate, remainder = _payload_lexical_path(project_root, relative)
    _inspect_payload_components(root, candidate)
    raw = _read_payload_windows(candidate) if os.name == "nt" else _read_payload_posix(root, remainder)
    # Recheck the lexical chain while the Windows handle is replacement-denying;
    # POSIX parent/final handles already pin the opened objects.
    _inspect_payload_components(root, candidate)
    return raw


def validate_payload_path(project_root: Path, relative: str, expected_sha256: str) -> bytes:
    """Return the exact securely opened bytes after path and digest validation."""
    _string(relative, "payload_path")
    if not HEX64.fullmatch(expected_sha256 or ""):
        _fail("INVALID_DIGEST", "payload_sha256")
    raw = _secure_read_payload_bytes(project_root, relative)
    if digest(raw) != expected_sha256:
        _fail("PAYLOAD_DIGEST_MISMATCH", relative)
    return raw


def envelope_request_hash(envelope: Mapping[str, Any]) -> str:
    return digest(envelope)


def _role_payload(envelope: Mapping[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        _fail("INVALID_PAYLOAD", "ROLE payload must be an object")
    message_type = envelope["message_type"]
    if message_type == "HANDSHAKE":
        _exact(payload, ("probe_kind", "run_id", "nonce"))
        if payload["probe_kind"] != "CROSS_CLI_HANDSHAKE":
            _fail("UNKNOWN_PROBE_KIND", str(payload["probe_kind"]))
        if payload["run_id"] != envelope["run_id"] or payload["nonce"] != envelope["nonce"]:
            _fail("INVALID_ECHO", "ROLE handshake run_id/nonce")
    elif message_type == "ASSIGNMENT":
        _exact(payload, (
            "spec_path", "spec_sha256", "spec_version", "leader_binding_id",
            "executor_binding_id", "required_skill",
        ))
        for key in ("spec_path", "leader_binding_id", "executor_binding_id", "required_skill"):
            _string(payload[key], key)
        if not HEX64.fullmatch(payload["spec_sha256"] or "") or not isinstance(payload["spec_version"], int) or payload["spec_version"] < 1:
            _fail("INVALID_ASSIGNMENT", "spec digest/version")
    elif message_type == "RECEIPT_ACK":
        cross_fields = ("probe_kind", "run_id", "nonce", "original_message_id", "canonical_request_hash")
        generic_fields = ("original_message_id", "original_nonce", "canonical_request_hash", "accepted", "operation_key")
        if set(payload) == set(cross_fields):
            _exact(payload, cross_fields)
            if payload["probe_kind"] != "CROSS_CLI_HANDSHAKE" or payload["run_id"] != envelope["run_id"] or payload["nonce"] != envelope["nonce"]:
                _fail("INVALID_ECHO", "cross-CLI receipt")
        else:
            _exact(payload, generic_fields)
            if payload["accepted"] is not True:
                _fail("RECEIPT_REJECTED", envelope["message_id"])
            _string(payload["original_nonce"], "original_nonce")
            _string(payload["operation_key"], "operation_key")
        _string(payload["original_message_id"], "original_message_id")
        if not HEX64.fullmatch(payload["canonical_request_hash"] or "") or envelope["reply_to"] != payload["original_message_id"]:
            _fail("INVALID_ECHO", "receipt request identity")
    elif message_type == "COMPLETION":
        _exact(payload, (
            "original_message_id", "original_nonce", "canonical_request_hash",
            "status", "result_path", "result_sha256",
        ))
        if payload["status"] not in TERMINAL_STATES:
            _fail("INVALID_COMPLETION", str(payload["status"]))
        _string(payload["original_message_id"], "original_message_id")
        _string(payload["original_nonce"], "original_nonce")
        _string(payload["result_path"], "result_path", nullable=True)
        _string(payload["result_sha256"], "result_sha256", nullable=True)
        if not HEX64.fullmatch(payload["canonical_request_hash"] or "") or envelope["reply_to"] != payload["original_message_id"]:
            _fail("INVALID_ECHO", "completion request identity")
        if (payload["result_path"] is None) != (payload["result_sha256"] is None) or (payload["result_sha256"] is not None and not HEX64.fullmatch(payload["result_sha256"])):
            _fail("INVALID_COMPLETION", "result path/digest")
    elif message_type == "REVIEW_REQUEST":
        _exact(payload, ("plan_path", "review_iteration", "review_fingerprint", "review_scope"))
        _string(payload["plan_path"], "plan_path")
        if not isinstance(payload["review_iteration"], int) or payload["review_iteration"] < 1 or not HEX64.fullmatch(payload["review_fingerprint"] or ""):
            _fail("INVALID_REVIEW_REQUEST", "iteration/fingerprint")
        if not isinstance(payload["review_scope"], list) or not payload["review_scope"] or any(not isinstance(item, str) or not item for item in payload["review_scope"]):
            _fail("INVALID_REVIEW_REQUEST", "review scope")
    elif message_type == "REVIEW_VERDICT":
        _exact(payload, (
            "original_message_id", "original_nonce", "canonical_request_hash",
            "verdict", "review_path", "review_sha256",
        ))
        if payload["verdict"] not in ("PASS", "REQUEST_CHANGES", "STALE"):
            _fail("INVALID_REVIEW_VERDICT", str(payload["verdict"]))
        for key in ("original_message_id", "original_nonce", "review_path"):
            _string(payload[key], key)
        if not HEX64.fullmatch(payload["canonical_request_hash"] or "") or not HEX64.fullmatch(payload["review_sha256"] or "") or envelope["reply_to"] != payload["original_message_id"]:
            _fail("INVALID_ECHO", "review verdict request identity")
    elif message_type == "LEADER_ACTIVATED":
        _exact(payload, (
            "migration_id", "leader_generation", "leader_binding_id",
            "executor_parent_binding_id", "reviewer_parent_binding_id", "roles_sha256",
        ))
        for key in ("migration_id", "leader_binding_id", "executor_parent_binding_id", "reviewer_parent_binding_id"):
            _string(payload[key], key)
        if payload["migration_id"] != envelope["migration_id"] or payload["leader_generation"] != envelope["leader_generation"] or not HEX64.fullmatch(payload["roles_sha256"] or ""):
            _fail("INVALID_ACTIVATION", "migration/generation/roles digest")


def _candidate_payload(envelope: Mapping[str, Any], context: Mapping[str, Any] | None) -> None:
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        _fail("INVALID_PAYLOAD", "candidate payload must be inline object")
    message_type = envelope["message_type"]
    common_echo = ("original_message_id", "original_nonce", "canonical_request_hash", "migration_id", "probe_id")
    if message_type == "HANDSHAKE":
        fields = (
            "probe_id", "target_generation", "compatibility_row_id",
            "compatibility_digest", "candidate_binding_id", "candidate_session_id",
            "candidate_resume_lineage", "handoff_digest", "ordered_probe_ids",
            "operation_keys",
        )
        _exact(payload, fields)
        if envelope["reply_to"] is not None:
            _fail("INVALID_PREDECESSOR", "handshake has no predecessor")
        if payload["target_generation"] != envelope["leader_generation"] + 1:
            _fail("INVALID_TARGET_GENERATION", str(payload["target_generation"]))
        for key in ("probe_id", "compatibility_row_id", "candidate_binding_id", "candidate_session_id", "candidate_resume_lineage", "handoff_digest"):
            _string(payload[key], key)
        for key in ("compatibility_digest", "handoff_digest"):
            if not HEX64.fullmatch(payload[key]):
                _fail("INVALID_DIGEST", key)
        if not isinstance(payload["ordered_probe_ids"], list) or not payload["ordered_probe_ids"]:
            _fail("INVALID_PROBE_ORDER", "ordered_probe_ids")
        if len(payload["ordered_probe_ids"]) != len(set(payload["ordered_probe_ids"])):
            _fail("INVALID_PROBE_ORDER", "duplicates")
        if not isinstance(payload["operation_keys"], dict) or set(payload["operation_keys"]) != set(payload["ordered_probe_ids"]):
            _fail("INVALID_OPERATION_KEYS", "must map every ordered probe")
        if any(not isinstance(value, str) or not value for value in payload["operation_keys"].values()) or len(set(payload["operation_keys"].values())) != len(payload["operation_keys"]):
            _fail("INVALID_OPERATION_KEYS", "keys must be unique non-empty strings")
    elif message_type == "RECEIPT_ACK":
        _exact(payload, (*common_echo, "candidate_identity"))
        identity = payload["candidate_identity"]
        if not isinstance(identity, dict):
            _fail("INVALID_IDENTITY", "candidate_identity")
        _validate_party(identity, "candidate_identity")
    else:
        _exact(payload, (*common_echo, "probes", "candidate_binding_readback", "report_delivery_readback"))
        if not isinstance(payload["probes"], list):
            _fail("INVALID_PROBES", "probes must be a list")
        for probe in payload["probes"]:
            _exact(probe, ("probe_id", "operation_key", "effect_policy", "terminal_outcome", "evidence_digest"))
            if probe["effect_policy"] != EffectPolicy.RECONCILABLE_IDEMPOTENT.value:
                _fail("INVALID_EFFECT_POLICY", probe["probe_id"])
            if probe["terminal_outcome"] != "SUCCEEDED":
                _fail("PROBE_NOT_SUCCESSFUL", probe["probe_id"])
            if not HEX64.fullmatch(probe["evidence_digest"]):
                _fail("INVALID_DIGEST", "evidence_digest")
        for name in ("candidate_binding_readback", "report_delivery_readback"):
            readback = payload[name]
            if not isinstance(readback, dict):
                _fail("INVALID_READBACK", name)
        _exact(payload["candidate_binding_readback"], ("binding_id",))
        _string(payload["candidate_binding_readback"]["binding_id"], "candidate_binding_readback.binding_id")
        _exact(payload["report_delivery_readback"], ("operation_key",))
        _string(payload["report_delivery_readback"]["operation_key"], "report_delivery_readback.operation_key")
    if context:
        if envelope["migration_id"] != context.get("migration_id") or envelope["leader_generation"] != context.get("source_generation"):
            _fail("CANDIDATE_CONTEXT_MISMATCH", "migration/generation")
        candidate = context.get("candidate", {})
        if message_type == "HANDSHAKE":
            if payload["candidate_binding_id"] != candidate.get("binding_id") or payload["candidate_session_id"] != candidate.get("session_id") or payload["candidate_resume_lineage"] != candidate.get("resume_lineage"):
                _fail("CANDIDATE_CONTEXT_MISMATCH", "candidate identity")
            if payload["compatibility_digest"] != context.get("compatibility_digest"):
                _fail("CANDIDATE_CONTEXT_MISMATCH", "compatibility digest")
        elif message_type == "RECEIPT_ACK":
            identity = payload["candidate_identity"]
            if identity["binding_id"] != candidate.get("binding_id") or identity["session_id"] != candidate.get("session_id", candidate.get("active_session_id")):
                _fail("CANDIDATE_CONTEXT_MISMATCH", "receipt identity")


def validate_envelope(envelope: Mapping[str, Any], *, project_root: Path | None = None, candidate_context: Mapping[str, Any] | None = None, runtime_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        _fail("INVALID_ENVELOPE", "document must be object")
    _exact(envelope, ENVELOPE_FIELDS)
    if envelope["schema_version"] != 1:
        _fail("UNSUPPORTED_SCHEMA", "envelope schema must be 1")
    if envelope["authority_scope"] not in ("ROLE", "CANDIDATE_VALIDATION"):
        _fail("INVALID_SCOPE", str(envelope["authority_scope"]))
    if envelope["message_type"] not in MESSAGE_TYPES:
        _fail("UNKNOWN_MESSAGE_TYPE", str(envelope["message_type"]))
    for key in ("message_id", "run_id", "nonce"):
        _string(envelope[key], key)
    _string(envelope["reply_to"], "reply_to", nullable=True)
    _string(envelope["migration_id"], "migration_id", nullable=True)
    if not isinstance(envelope["leader_generation"], int) or envelope["leader_generation"] < 0:
        _fail("INVALID_GENERATION", str(envelope["leader_generation"]))
    _validate_party(envelope["sender"], "sender")
    _validate_party(envelope["recipient"], "recipient")
    created = _parse_timestamp(envelope["created_at"])
    expires = _parse_timestamp(envelope["expires_at"])
    if created >= expires:
        _fail("INVALID_EXPIRY", "expires_at must follow created_at")
    has_inline = envelope["payload"] is not None
    has_path = envelope["payload_path"] is not None
    if has_inline == has_path:
        _fail("INVALID_PAYLOAD", "exactly one payload representation is required")
    payload_value = envelope["payload"]
    if has_path:
        if project_root is None:
            _fail("PROJECT_ROOT_REQUIRED", "durable payload reference")
        payload_bytes = validate_payload_path(project_root, envelope["payload_path"], envelope["payload_sha256"])
        try:
            payload_value = json.loads(payload_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            _fail("INVALID_JSON", f"{envelope['payload_path']}: {exc}")
    elif envelope["payload_sha256"] != digest(envelope["payload"]):
        _fail("PAYLOAD_DIGEST_MISMATCH", "inline payload")
    if envelope["authority_scope"] == "CANDIDATE_VALIDATION":
        if envelope["message_type"] not in CANDIDATE_TYPES or envelope["migration_id"] is None or not has_inline:
            _fail("INVALID_CANDIDATE_MESSAGE", str(envelope["message_type"]))
        _candidate_payload(envelope, candidate_context)
    else:
        _role_payload(envelope, payload_value)
    if runtime_context:
        if envelope["run_id"] != runtime_context.get("run_id"):
            _fail("WRONG_RUN", envelope["run_id"])
        if envelope["leader_generation"] != runtime_context.get("leader_generation"):
            _fail("STALE_GENERATION", str(envelope["leader_generation"]))
        now_value = runtime_context.get("now")
        if not isinstance(now_value, str) or _parse_timestamp(now_value) >= expires:
            _fail("EXPIRED", envelope["message_id"])
        expected_recipient = runtime_context.get("recipient")
        if not isinstance(expected_recipient, Mapping) or any(envelope["recipient"].get(key) != expected_recipient.get(key) for key in PARTY_FIELDS):
            _fail("WRONG_RECIPIENT", envelope["message_id"])
        allowed_senders = runtime_context.get("allowed_senders")
        if not isinstance(allowed_senders, list) or envelope["sender"]["binding_id"] not in allowed_senders:
            _fail("WRONG_SENDER", envelope["sender"]["binding_id"])
        predecessors = runtime_context.get("predecessors", {})
        required_predecessor = {"RECEIPT_ACK", "COMPLETION", "REVIEW_VERDICT"}
        if envelope["message_type"] in required_predecessor:
            prior = predecessors.get(envelope["reply_to"]) if isinstance(predecessors, Mapping) else None
            allowed_prior = {
                "RECEIPT_ACK": ("HANDSHAKE", "ASSIGNMENT", "REVIEW_REQUEST", "LEADER_ACTIVATED"),
                "COMPLETION": ("HANDSHAKE", "ASSIGNMENT"),
                "REVIEW_VERDICT": ("REVIEW_REQUEST",),
            }
            if not isinstance(prior, Mapping) or prior.get("message_type") not in allowed_prior[envelope["message_type"]]:
                _fail("INVALID_PREDECESSOR", envelope["message_type"])
    return copy.deepcopy(envelope)


def validate_cross_cli_handshake_reply(request: Mapping[str, Any], reply: Mapping[str, Any]) -> None:
    """Validate the binding-report echo for a ROLE-scoped cross-CLI probe."""
    original = validate_envelope(request)
    if original["authority_scope"] != "ROLE" or original["message_type"] != "HANDSHAKE":
        _fail("INVALID_HANDSHAKE", "request is not a ROLE HANDSHAKE")
    response = validate_envelope(reply)
    if response["authority_scope"] != "ROLE" or response["message_type"] != "RECEIPT_ACK":
        _fail("INVALID_HANDSHAKE_REPLY", "expected ROLE RECEIPT_ACK")
    payload = response["payload"]
    if not isinstance(payload, dict):
        _fail("INVALID_HANDSHAKE_REPLY", "inline payload required")
    _exact(payload, ("probe_kind", "run_id", "nonce", "original_message_id", "canonical_request_hash"))
    if payload != {
        "probe_kind": "CROSS_CLI_HANDSHAKE",
        "run_id": original["run_id"],
        "nonce": original["nonce"],
        "original_message_id": original["message_id"],
        "canonical_request_hash": envelope_request_hash(original),
    } or response["reply_to"] != original["message_id"]:
        _fail("INVALID_ECHO", "cross-CLI handshake reply")
    if response["sender"] != original["recipient"] or response["recipient"] != original["sender"]:
        _fail("INVALID_PARTY", "cross-CLI handshake reply")


def validate_candidate_sequence(messages: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> None:
    if [m.get("message_type") for m in messages] != list(CANDIDATE_TYPES):
        _fail("INVALID_MESSAGE_ORDER", "expected HANDSHAKE, RECEIPT_ACK, COMPLETION")
    validated = [validate_envelope(item, candidate_context=context) for item in messages]
    handshake, receipt, completion = validated
    old_leader = context.get("old_leader")
    candidate_identity = context.get("candidate")
    if isinstance(old_leader, Mapping):
        expected_old = {key: old_leader.get(key) for key in PARTY_FIELDS}
        if handshake["sender"] != expected_old:
            _fail("CANDIDATE_CONTEXT_MISMATCH", "old Leader identity")
    if isinstance(candidate_identity, Mapping):
        expected_candidate = {
            "role": "leader",
            "cli_tool": candidate_identity.get("cli_tool", "claude"),
            "session_id": candidate_identity.get("session_id", candidate_identity.get("active_session_id")),
            "binding_id": candidate_identity.get("binding_id"),
        }
        if handshake["recipient"] != expected_candidate:
            _fail("CANDIDATE_CONTEXT_MISMATCH", "candidate party identity")
    request_hash = envelope_request_hash(handshake)
    for reply in (receipt, completion):
        payload = reply["payload"]
        if reply["reply_to"] != handshake["message_id"] or payload["original_message_id"] != handshake["message_id"] or payload["original_nonce"] != handshake["nonce"] or payload["canonical_request_hash"] != request_hash:
            _fail("INVALID_ECHO", reply["message_type"])
        if payload["migration_id"] != handshake["migration_id"] or payload["probe_id"] != handshake["payload"]["probe_id"]:
            _fail("INVALID_ECHO", "migration/probe")
        if reply["sender"] != handshake["recipient"] or reply["recipient"] != handshake["sender"]:
            _fail("INVALID_PARTY", reply["message_type"])
    probes = completion["payload"]["probes"]
    expected_ids = handshake["payload"]["ordered_probe_ids"]
    if [p["probe_id"] for p in probes] != expected_ids:
        _fail("INVALID_PROBE_ORDER", "completion")
    keys = handshake["payload"]["operation_keys"]
    if any(p["operation_key"] != keys[p["probe_id"]] for p in probes):
        _fail("INVALID_OPERATION_KEYS", "completion")
    if completion["payload"]["candidate_binding_readback"]["binding_id"] != handshake["payload"]["candidate_binding_id"]:
        _fail("INVALID_READBACK", "candidate_binding_readback")
    if completion["payload"]["report_delivery_readback"]["operation_key"] not in keys.values():
        _fail("INVALID_READBACK", "report_delivery_readback")


LEDGER_REQUIRED = (
    "schema_version", "record_sequence", "previous_record_digest", "record_digest",
    "request_key", "request_sha256", "writer_binding_id", "writer_session_id",
    "writer_generation", "processing_state", "operation_key", "effect_policy",
    "readback_predicate", "outcome", "recorded_at",
)


def _record_digest(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_digest", None)
    return digest(unsigned)


def validate_ledger_records(records: Sequence[Mapping[str, Any]], *, allow_session_replacement: bool = False) -> None:
    previous = "0" * 64
    states: dict[tuple[Any, ...], str] = {}
    hashes: dict[tuple[Any, ...], str] = {}
    writers_by_generation: dict[int, tuple[str, str]] = {}
    previous_generation = -1
    for sequence, record in enumerate(records, 1):
        if not isinstance(record, dict):
            _fail("LEDGER_CORRUPT", f"record {sequence}")
        _exact(record, LEDGER_REQUIRED)
        if record["schema_version"] != 1 or record["record_sequence"] != sequence:
            _fail("LEDGER_CORRUPT", f"sequence {sequence}")
        if record["previous_record_digest"] != previous or record["record_digest"] != _record_digest(record):
            _fail("LEDGER_CORRUPT", f"hash chain at {sequence}")
        key = tuple(record["request_key"])
        if len(key) != 4 or not isinstance(key[0], str) or not isinstance(key[1], str) or not isinstance(key[2], int) or not isinstance(key[3], str) or record["processing_state"] not in PROCESSING_STATES:
            _fail("LEDGER_CORRUPT", f"state/key at {sequence}")
        if not HEX64.fullmatch(record["request_sha256"]) or not isinstance(record["writer_generation"], int) or record["writer_generation"] < previous_generation:
            _fail("LEDGER_CORRUPT", f"digest/generation at {sequence}")
        writer = (record["writer_binding_id"], record["writer_session_id"])
        prior_writer = writers_by_generation.setdefault(record["writer_generation"], writer)
        if prior_writer != writer and (not allow_session_replacement or prior_writer[0] != writer[0]):
            _fail("MULTIPLE_WRITERS", f"generation {record['writer_generation']}")
        previous_generation = record["writer_generation"]
        if record["effect_policy"] not in tuple(item.value for item in EffectPolicy):
            _fail("LEDGER_CORRUPT", f"effect policy at {sequence}")
        if record["effect_policy"] == EffectPolicy.RECONCILABLE_IDEMPOTENT.value and (not record["operation_key"] or not record["readback_predicate"]):
            _fail("LEDGER_CORRUPT", f"reconciliation at {sequence}")
        normalize_timestamp(record["recorded_at"])
        old_hash = hashes.setdefault(key, record["request_sha256"])
        if old_hash != record["request_sha256"]:
            _fail("REQUEST_HASH_CONFLICT", str(key))
        old = states.get(key)
        if old is None and record["processing_state"] != "RECEIVED":
            _fail("INVALID_STATE_TRANSITION", f"{key}: initial")
        if old == "RECEIVED" and record["processing_state"] != "EXECUTING":
            _fail("INVALID_STATE_TRANSITION", f"{key}: {old}")
        if old == "EXECUTING" and record["processing_state"] not in TERMINAL_STATES:
            _fail("INVALID_STATE_TRANSITION", f"{key}: {old}")
        if old in TERMINAL_STATES:
            _fail("INVALID_STATE_TRANSITION", f"{key}: terminal")
        states[key] = record["processing_state"]
        previous = record["record_digest"]


def read_ledger(path: Path, *, recover_torn_final: bool = True, allow_session_replacement: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            if recover_torn_final and index == len(lines) - 1:
                quarantine = path.with_name(path.name + ".torn." + digest(line) + ".quarantine")
                atomic_write(quarantine, line)
                atomic_write(path, b"".join(canonical_bytes(item) + b"\n" for item in records))
                break
            _fail("LEDGER_CORRUPT", f"invalid JSON record {index + 1}")
        canonical_line = canonical_bytes(record) + b"\n"
        if line != canonical_line:
            if recover_torn_final and index == len(lines) - 1 and not line.endswith(b"\n"):
                quarantine = path.with_name(path.name + ".torn." + digest(line) + ".quarantine")
                atomic_write(quarantine, line)
                atomic_write(path, b"".join(canonical_bytes(item) + b"\n" for item in records))
                break
            _fail("LEDGER_NON_CANONICAL", f"record {index + 1}")
        records.append(record)
    validate_ledger_records(records, allow_session_replacement=allow_session_replacement)
    return records


def _writer_for_role(role_state: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    validate_role_state(role_state)
    return role_state["roles"][ROLES.index(role)]


class Ledger:
    """Fenced, hash-chained, atomic logical append ledger."""

    def __init__(self, path: Path, authority: Callable[[], Mapping[str, Any]], role: str, writer_binding_id: str, writer_session_id: str, generation: int):
        self.path, self.authority, self.role = path, authority, role
        self.writer_binding_id, self.writer_session_id, self.generation = writer_binding_id, writer_session_id, generation

    def _check_writer(self) -> None:
        descriptor = _writer_for_role(self.authority(), self.role)
        if descriptor["binding_id"] != self.writer_binding_id or descriptor["session_id"] != self.writer_session_id or descriptor["leader_generation"] != self.generation or descriptor["authority_state"] not in ("ACTIVE", "FROZEN"):
            _fail("STALE_WRITER", self.role)

    def records(self) -> list[dict[str, Any]]:
        return self._read_records()

    def _read_records(self) -> list[dict[str, Any]]:
        return read_ledger(self.path)

    def append(self, request_key: Sequence[str | int], request_sha256: str, state: str, *, operation_key: str | None = None, effect_policy: EffectPolicy = EffectPolicy.PURE, readback_predicate: str | None = None, outcome: Any = None, recorded_at: str | None = None) -> dict[str, Any]:
        if len(request_key) != 4 or not HEX64.fullmatch(request_sha256):
            _fail("INVALID_REQUEST", "key/hash")
        if state not in PROCESSING_STATES:
            _fail("INVALID_STATE", state)
        if effect_policy == EffectPolicy.RECONCILABLE_IDEMPOTENT and (not operation_key or not readback_predicate):
            _fail("MISSING_RECONCILIATION", "operation key and readback predicate required")
        with exclusive_lock(self.path):
            self._check_writer()
            records = self._read_records()
            same = [r for r in records if tuple(r["request_key"]) == tuple(request_key)]
            if same and same[0]["request_sha256"] != request_sha256:
                _fail("REQUEST_HASH_CONFLICT", str(request_key))
            if same and same[-1]["processing_state"] in TERMINAL_STATES:
                return copy.deepcopy(same[-1])
            if same and same[-1]["processing_state"] == state:
                return copy.deepcopy(same[-1])
            record: dict[str, Any] = {
                "schema_version": 1,
                "record_sequence": len(records) + 1,
                "previous_record_digest": records[-1]["record_digest"] if records else "0" * 64,
                "record_digest": "",
                "request_key": list(request_key),
                "request_sha256": request_sha256,
                "writer_binding_id": self.writer_binding_id,
                "writer_session_id": self.writer_session_id,
                "writer_generation": self.generation,
                "processing_state": state,
                "operation_key": operation_key,
                "effect_policy": effect_policy.value,
                "readback_predicate": readback_predicate,
                "outcome": outcome,
                "recorded_at": normalize_timestamp(recorded_at or dt.datetime.now(dt.timezone.utc).isoformat()),
            }
            record["record_digest"] = _record_digest(record)
            candidate = [*records, record]
            validate_ledger_records(candidate, allow_session_replacement=isinstance(self, CandidateLedger))
            old_bytes = self.path.read_bytes() if self.path.exists() else b""
            atomic_write(self.path, old_bytes + canonical_bytes(record) + b"\n")
            return copy.deepcopy(record)


class CandidateLedger(Ledger):
    """Ledger fenced by a PREPARED/VALIDATED migration journal."""

    def __init__(self, path: Path, journal: Callable[[], Mapping[str, Any]], writer_binding_id: str, writer_session_id: str, target_generation: int):
        self.path, self.journal = path, journal
        self.writer_binding_id, self.writer_session_id, self.generation = writer_binding_id, writer_session_id, target_generation
        self.role = "leader"
        initial = validate_migration_journal(journal())
        self.migration_id = initial["migration_id"]
        self.resume_lineage = initial["candidate"]["resume_lineage"]
        self.compatibility_digest = initial["compatibility_digest"]

    def _check_writer(self) -> None:
        journal = self.journal()
        validate_migration_journal(journal)
        candidate = journal["candidate"]
        if journal["migration_id"] != self.migration_id or journal["compatibility_digest"] != self.compatibility_digest or candidate["resume_lineage"] != self.resume_lineage:
            _fail("CANDIDATE_IDENTITY_CHANGED", self.writer_binding_id)
        if journal["phase"] not in ("PREPARED", "VALIDATED") or journal.get("candidate_ledger_closed") or candidate["binding_id"] != self.writer_binding_id or candidate["active_session_id"] != self.writer_session_id or journal["target_generation"] != self.generation:
            _fail("STALE_CANDIDATE", self.writer_binding_id)

    def _read_records(self) -> list[dict[str, Any]]:
        # Session replacement is authorized only after _check_writer verifies the
        # journal's same binding/resume lineage and sole active session.
        return read_ledger(self.path, allow_session_replacement=True)

    def append_envelope(self, envelope: Mapping[str, Any], state: str, context: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        self._check_writer()
        validated = validate_envelope(envelope, candidate_context=context)
        if validated["message_type"] not in CANDIDATE_TYPES:
            _fail("INVALID_CANDIDATE_MESSAGE", validated["message_type"])
        sender = validated["sender"]
        key = (sender["binding_id"], validated["run_id"], validated["leader_generation"], validated["message_id"])
        return self.append(key, envelope_request_hash(validated), state, **kwargs)


def reconcile_unresolved(policy: EffectPolicy, observation: Observation, *, adapter_honors_key: bool, execute: Callable[[], Any] | None = None) -> tuple[str, Any]:
    """Return terminal/retry result for an unresolved EXECUTING operation."""
    if policy == EffectPolicy.PURE:
        return "SUCCEEDED", execute() if execute else None
    if policy == EffectPolicy.UNRECONCILABLE or observation == Observation.UNKNOWN:
        return "INDETERMINATE", None
    if observation == Observation.COMPLETE:
        return "SUCCEEDED", {"recovered": True}
    if not adapter_honors_key:
        return "INDETERMINATE", None
    if execute is None:
        _fail("MISSING_EXECUTOR", "proven-absent effect")
    return "SUCCEEDED", execute()


def operation_key(migration_id: str, probe_id: str, operation_name: str) -> str:
    """Derive an unambiguous stable external-effect correlation key."""
    for name, value in (("migration_id", migration_id), ("probe_id", probe_id), ("operation_name", operation_name)):
        _string(value, name)
    return "agent-team-effect/v1:" + digest([migration_id, probe_id, operation_name])


def recover_executing(ledger: "Ledger", executing_record: Mapping[str, Any], observation: Observation, *, adapter_honors_key: bool, execute: Callable[[], Any] | None = None, recorded_at: str | None = None) -> dict[str, Any]:
    """Reconcile one durable EXECUTING record and persist its terminal result."""
    if executing_record.get("processing_state") != "EXECUTING":
        _fail("NOT_EXECUTING", str(executing_record.get("processing_state")))
    try:
        policy = EffectPolicy(executing_record["effect_policy"])
    except (KeyError, ValueError):
        _fail("INVALID_EFFECT_POLICY", str(executing_record.get("effect_policy")))
    state, outcome = reconcile_unresolved(policy, observation, adapter_honors_key=adapter_honors_key, execute=execute)
    return ledger.append(
        executing_record["request_key"], executing_record["request_sha256"], state,
        operation_key=executing_record.get("operation_key"), effect_policy=policy,
        readback_predicate=executing_record.get("readback_predicate"), outcome=outcome,
        recorded_at=recorded_at,
    )


JOURNAL_FIELDS = (
    "schema_version", "run_id", "migration_id", "phase", "source_generation",
    "target_generation", "old_leader", "candidate", "compatibility_row_id",
    "compatibility_digest", "before_state", "expected_binding_parents",
    "observed_binding_parents", "steps", "recovery_decision",
    "candidate_ledger_digest", "candidate_ledger_closed",
)

JOURNALED_EFFECT_INTENT_FIELDS = (
    "operation_key", "desired_sha256", "readback_predicate",
)
JOURNALED_EFFECT_APPLIED_FIELDS = (
    *JOURNALED_EFFECT_INTENT_FIELDS, "readback", "readback_sha256", "outcome",
)


def validate_applied_effect_correlation(
    operation: str, before: Any, after: Any,
) -> dict[str, Any]:
    """Validate the complete durable correlation tuple for an applied effect."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        _fail("INVALID_APPLIED_CORRELATION", operation)
    missing_before = [field for field in JOURNALED_EFFECT_INTENT_FIELDS if field not in before]
    missing_after = [field for field in JOURNALED_EFFECT_APPLIED_FIELDS if field not in after]
    if missing_before or missing_after:
        _fail(
            "INVALID_APPLIED_CORRELATION",
            f"{operation}:missing:{','.join((*missing_before, *missing_after))}",
        )
    if before["operation_key"] != operation or after["operation_key"] != operation:
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:operation_key")
    if (
        not isinstance(before["desired_sha256"], str)
        or not HEX64.fullmatch(before["desired_sha256"])
        or after["desired_sha256"] != before["desired_sha256"]
    ):
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:desired_sha256")
    if (
        not isinstance(before["readback_predicate"], str)
        or not before["readback_predicate"]
        or after["readback_predicate"] != before["readback_predicate"]
    ):
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:readback_predicate")
    if after["outcome"] != "SUCCEEDED":
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:outcome")
    if not isinstance(after["readback"], Mapping):
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:readback")
    if after["readback_sha256"] != digest(after["readback"]):
        _fail("INVALID_APPLIED_CORRELATION", f"{operation}:readback_sha256")
    return copy.deepcopy(dict(after))


def validate_migration_journal(journal: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(journal, dict):
        _fail("INVALID_JOURNAL", "object required")
    _exact(journal, JOURNAL_FIELDS)
    if journal["schema_version"] != 1:
        _fail("UNSUPPORTED_SCHEMA", "migration schema must be 1")
    for key in ("run_id", "migration_id", "compatibility_row_id"):
        _string(journal[key], key)
    if journal["phase"] not in MIGRATION_PHASES:
        _fail("INVALID_PHASE", str(journal["phase"]))
    if not isinstance(journal["source_generation"], int) or journal["target_generation"] != journal["source_generation"] + 1:
        _fail("INVALID_TARGET_GENERATION", str(journal["target_generation"]))
    if not HEX64.fullmatch(journal["compatibility_digest"]):
        _fail("INVALID_DIGEST", "compatibility_digest")
    for identity in ("old_leader", "candidate"):
        if not isinstance(journal[identity], dict):
            _fail("INVALID_IDENTITY", identity)
    candidate = journal["candidate"]
    _exact(candidate, ("binding_id", "active_session_id", "resume_lineage"))
    for key in candidate:
        _string(candidate[key], f"candidate.{key}", nullable=journal["phase"] in ("CREATED", "PREFLIGHTED", "ABORTED"))
    if not isinstance(journal["steps"], list):
        _fail("INVALID_STEPS", "steps must be ordered list")
    previous_phase_index = -1
    applied_phases: set[str] = set()
    before_operations: dict[tuple[str, str], Mapping[str, Any]] = {}
    completed_operations: set[tuple[str, str]] = set()
    for sequence, step in enumerate(journal["steps"], 1):
        _exact(step, ("sequence", "phase", "operation", "status", "before", "after", "recorded_at"))
        if step["sequence"] != sequence or step["phase"] not in MIGRATION_PHASES:
            _fail("INVALID_STEPS", "sequence/phase")
        if step["phase"] in PHASE_ORDER:
            if PHASE_ORDER[step["phase"]] < previous_phase_index:
                _fail("PHASE_REGRESSION", step["phase"])
            previous_phase_index = PHASE_ORDER[step["phase"]]
        if step["status"] not in ("BEFORE", "APPLIED", "RECOVERED"):
            _fail("INVALID_STEP_STATUS", step["status"])
        step_key = (step["phase"], step["operation"])
        if step["status"] == "BEFORE":
            correlation_fields = {"desired_sha256", "readback_predicate", "readback_sha256"}
            prior_before = before_operations.get(step_key)
            correlated_retry = (
                prior_before is not None
                and (
                    (
                        isinstance(prior_before["before"], Mapping)
                        and correlation_fields.intersection(prior_before["before"])
                    )
                    or (
                        isinstance(step["before"], Mapping)
                        and correlation_fields.intersection(step["before"])
                    )
                )
            )
            if step_key in completed_operations or correlated_retry:
                _fail("AMBIGUOUS_STEP_PAIR", f"{step['phase']}:{step['operation']}")
            before_operations[step_key] = step
        else:
            if step_key not in before_operations:
                _fail("MISSING_BEFORE_STEP", f"{step['phase']}:{step['operation']}")
            if step_key in completed_operations:
                _fail("AMBIGUOUS_STEP_PAIR", f"{step['phase']}:{step['operation']}")
            before = before_operations[step_key]["before"]
            after = step["after"]
            correlation_fields = {"desired_sha256", "readback_predicate", "readback_sha256"}
            if (
                isinstance(before, Mapping) and correlation_fields.intersection(before)
            ) or (
                isinstance(after, Mapping) and correlation_fields.intersection(after)
            ):
                validate_applied_effect_correlation(step["operation"], before, after)
            completed_operations.add(step_key)
            applied_phases.add(step["phase"])
        normalize_timestamp(step["recorded_at"])
    if journal["phase"] == "COMMITTED":
        missing_phases = [phase for phase in COMMIT_PHASES if phase not in applied_phases]
        if missing_phases:
            _fail("INCOMPLETE_COMMIT_JOURNAL", ",".join(missing_phases))
        if (
            journal["candidate_ledger_closed"] is not True
            or not HEX64.fullmatch(journal["candidate_ledger_digest"] or "")
            or journal["candidate_ledger_digest"] == "0" * 64
        ):
            _fail("INCOMPLETE_COMMIT_JOURNAL", "candidate ledger evidence")
        if journal["recovery_decision"] != "COMMIT":
            _fail("INCOMPLETE_COMMIT_JOURNAL", "recovery decision")
        if journal["expected_binding_parents"] != journal["observed_binding_parents"]:
            _fail("INCOMPLETE_COMMIT_JOURNAL", "parent reconciliation")
    if journal["phase"] == "ROLLED_BACK":
        if journal["candidate_ledger_closed"] is not True or not HEX64.fullmatch(journal["candidate_ledger_digest"] or ""):
            _fail("INCOMPLETE_ROLLBACK_JOURNAL", "candidate ledger evidence")
        if journal["recovery_decision"] != "ROLL_BACK":
            _fail("INCOMPLETE_ROLLBACK_JOURNAL", "recovery decision")
    return copy.deepcopy(journal)


class MigrationJournal:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        return validate_migration_journal(load_json(self.path))

    def write(self, journal: Mapping[str, Any]) -> str:
        validated = validate_migration_journal(journal)
        with exclusive_lock(self.path):
            if self.path.exists():
                current = self.load()
                if current["migration_id"] != validated["migration_id"]:
                    _fail("MIGRATION_MISMATCH", validated["migration_id"])
                if current["phase"] in ("COMMITTED", "ABORTED", "ROLLED_BACK") and validated != current:
                    _fail("TERMINAL_JOURNAL_IMMUTABLE", current["phase"])
                old_index = PHASE_ORDER.get(current["phase"], 99 if current["phase"] in ("COMMITTED", "ABORTED", "ROLLED_BACK") else -1)
                new_index = PHASE_ORDER.get(validated["phase"], 99)
                if old_index >= PHASE_ORDER["STATE_ACTIVATED"] and validated["phase"] == "ROLLED_BACK":
                    _fail("ROLLBACK_AFTER_ACTIVATION", "recovery must roll forward")
                if new_index < old_index:
                    _fail("PHASE_REGRESSION", validated["phase"])
            raw = canonical_bytes(validated) + b"\n"
            atomic_write(self.path, raw, backup=True)
        return hashlib.sha256(raw).hexdigest()

    def record_step(
        self, phase: str, operation: str, status: str, before: Any, after: Any,
        recorded_at: str, *, advance_phase: bool = True,
    ) -> dict[str, Any]:
        journal = self.load()
        step = {"sequence": len(journal["steps"]) + 1, "phase": phase, "operation": operation, "status": status, "before": before, "after": after, "recorded_at": normalize_timestamp(recorded_at)}
        journal["steps"].append(step)
        # BEFORE describes intent/in-flight work only.  The authoritative phase
        # remains the last durably completed effect until APPLIED/RECOVERED is
        # flushed, so a crash in the activation window cannot cross the point of
        # no return merely by writing intent.
        # A phase may contain multiple separately recoverable effects.  v9 uses
        # this for OLD_SUPERSEDED: final reconciliation is durably APPLIED first,
        # but the phase advances only after the distinct supersede effect is also
        # APPLIED.  BEFORE still never advances authority state.
        if status in ("APPLIED", "RECOVERED") and advance_phase:
            journal["phase"] = phase
        self.write(journal)
        return step

    def recovery_direction(self, *, role_state_path: Path | None = None) -> str:
        """Choose recovery from completed journal work plus durable activation state.

        A role-state path is required to recognize the narrow crash window after
        the activation file replacement but before its journal APPLIED record.
        The state is loaded and schema-validated here; callers cannot substitute a
        scalar "activation passed" assertion.
        """
        journal = self.load()
        completed = [
            step["phase"] for step in journal["steps"]
            if step["status"] in ("APPLIED", "RECOVERED") and step["phase"] in PHASE_ORDER
        ]
        effective_phase = completed[-1] if completed else journal["phase"]
        if effective_phase in PHASE_ORDER and PHASE_ORDER[effective_phase] >= PHASE_ORDER["STATE_ACTIVATED"]:
            return "ROLL_FORWARD"
        if role_state_path is None:
            return "ROLL_BACK"

        state = validate_role_state(load_json(role_state_path))
        if state["run_id"] != journal["run_id"]:
            _fail("ACTIVATION_READBACK_MISMATCH", "run_id")
        generation = state["leader_generation"]
        migration_id = state["migration_id"]
        state_phase = state["migration_phase"]
        if generation < journal["source_generation"]:
            _fail("ACTIVATION_READBACK_MISMATCH", "generation regression")
        if generation == journal["source_generation"]:
            if state_phase in PHASE_ORDER and PHASE_ORDER[state_phase] >= PHASE_ORDER["STATE_ACTIVATED"]:
                _fail("ACTIVATION_READBACK_MISMATCH", "activated phase at source generation")
            return "ROLL_BACK"
        if generation < journal["target_generation"] or migration_id != journal["migration_id"]:
            _fail("ACTIVATION_READBACK_MISMATCH", "migration/generation")
        if state_phase not in PHASE_ORDER or PHASE_ORDER[state_phase] < PHASE_ORDER["STATE_ACTIVATED"]:
            _fail("ACTIVATION_READBACK_MISMATCH", "target generation lacks activation phase")
        return "ROLL_FORWARD"

    def replace_candidate_session(self, new_session_id: str, resume_lineage: str, *, prior_session_exited: bool) -> dict[str, Any]:
        """Fence a restarted candidate while preserving its exact resume lineage."""
        _string(new_session_id, "new_session_id")
        journal = self.load()
        if journal["phase"] not in ("PREPARED", "VALIDATED") or journal["candidate_ledger_closed"]:
            _fail("CANDIDATE_RESTART_FORBIDDEN", journal["phase"])
        if not prior_session_exited:
            _fail("PRIOR_SESSION_ACTIVE", journal["candidate"]["active_session_id"])
        if resume_lineage != journal["candidate"]["resume_lineage"]:
            _fail("RESUME_LINEAGE_MISMATCH", resume_lineage)
        journal["candidate"]["active_session_id"] = new_session_id
        self.write(journal)
        return journal

    def close_candidate_ledger(self, ledger_path: Path, *, recovery_decision: str) -> str:
        """Atomically close and retain the candidate evidence stream.

        Candidate appenders use ``exclusive_lock(ledger_path)`` as their
        serialization boundary.  The same lock must remain held from the
        initial journal/ledger checks through validation, exact-byte capture,
        quarantine, and the durable journal close; otherwise an append can
        race in after validation and escape the digest recorded in the
        journal.
        """
        with exclusive_lock(ledger_path):
            journal = self.load()
            if journal["candidate_ledger_closed"]:
                _fail("CANDIDATE_LEDGER_CLOSED", journal["migration_id"])
            records = read_ledger(ledger_path, allow_session_replacement=True)

            # Every request represented in the final chain must have reached a
            # terminal state.  In particular, a trailing RECEIVED/EXECUTING
            # record is never eligible for close/quarantine.
            latest: dict[tuple[Any, ...], str] = {}
            for record in records:
                latest[tuple(record["request_key"])] = record["processing_state"]
            nonterminal = [key for key, state in latest.items() if state not in TERMINAL_STATES]
            if nonterminal:
                _fail("CANDIDATE_LEDGER_NONTERMINAL", str(nonterminal[0]))

            # Capture the exact canonical byte stream while the append lock is
            # still held.  The quarantine and journal digest are both bound to
            # these bytes, not to the final hash-chain node.
            raw = ledger_path.read_bytes() if ledger_path.exists() else b""
            final_digest = digest(raw)
            quarantine = ledger_path.with_name(ledger_path.name + "." + final_digest + ".quarantine")
            atomic_write(quarantine, raw)
            journal["candidate_ledger_digest"] = final_digest
            journal["candidate_ledger_closed"] = True
            journal["recovery_decision"] = recovery_decision
            self.write(journal)
            return final_digest


ADAPTER_OPERATIONS = (
    "resolveProject", "launchRole", "resumeRole", "deliverPrompt",
    "observeSession", "stopSession", "bindRole", "updateMilestone",
    "reportToLeader", "reconcile",
)
ADAPTER_ERROR_CODES = (
    "MISSING_CAPABILITY", "PERMISSION_DENIED", "NOT_FOUND", "CONFLICT",
    "TIMEOUT", "UNRELIABLE_DELIVERY", "UNSUPPORTED", "INDETERMINATE", "INTERNAL",
)

EFFECT_POLICIES: dict[str, tuple[EffectPolicy, str | None]] = {
    "parse_and_hash": (EffectPolicy.PURE, None),
    "local_candidate_ledger_write": (EffectPolicy.PURE, None),
    "deliverPrompt": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "prompt_output_has_probe_id_nonce_hash"),
    "candidate_binding_update": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "binding_has_exact_candidate_metadata"),
    "reportToLeader": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "report_observation_has_operation_key_probe_id_hash"),
    "receipt_delivery": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "report_observation_has_operation_key_probe_id_hash"),
    "completion_delivery": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "report_observation_has_operation_key_probe_id_hash"),
    "protected_binding_mutation": (EffectPolicy.RECONCILABLE_IDEMPOTENT, "binding_parent_matches_generation"),
    "arbitrary_external_effect": (EffectPolicy.UNRECONCILABLE, None),
}


def effect_policy_for(operation: str) -> tuple[EffectPolicy, str | None]:
    try:
        return EFFECT_POLICIES[operation]
    except KeyError:
        _fail("UNKNOWN_EFFECT_POLICY", operation)


def normalize_adapter_error(operation: str, error: BaseException | Mapping[str, Any]) -> dict[str, Any]:
    if operation not in ADAPTER_OPERATIONS:
        _fail("UNKNOWN_ADAPTER_OPERATION", operation)
    if isinstance(error, Mapping):
        code = str(error.get("code", "INTERNAL")).upper()
        message = str(error.get("message", code))
    else:
        code = "TIMEOUT" if isinstance(error, TimeoutError) else "PERMISSION_DENIED" if isinstance(error, PermissionError) else "NOT_FOUND" if isinstance(error, FileNotFoundError) else "INTERNAL"
        message = str(error) or error.__class__.__name__
    if code not in ADAPTER_ERROR_CODES:
        code = "INTERNAL"
    return {"schema_version": 1, "operation": operation, "code": code, "message": message}


def validate_adapter_capabilities(advertised: Iterable[str], required: Iterable[str]) -> tuple[bool, list[str]]:
    advertised_set = {item for item in advertised if isinstance(item, str)}
    missing = sorted(set(required) - advertised_set)
    return not missing, missing


def validate_readiness(expected_root: str | Path, cwd: str | Path, git_top_level: str | Path, skill_loaded: bool, assignment_received: bool, role_identity_stable: bool) -> dict[str, Any]:
    """Validate exact adapter readiness evidence without transport inference."""
    expected = Path(expected_root).resolve()
    observed_cwd = Path(cwd).resolve()
    observed_git = Path(git_top_level).resolve()
    if str(observed_cwd) != str(expected):
        _fail("WRONG_CWD", f"expected {expected}, got {observed_cwd}")
    if str(observed_git) != str(expected):
        _fail("WRONG_GIT_ROOT", f"expected {expected}, got {observed_git}")
    if skill_loaded is not True:
        _fail("SKILL_NOT_LOADED", "native role Skill discovery/trust failed")
    if assignment_received is not True:
        _fail("ASSIGNMENT_NOT_RECEIVED", "injected assignment was not observed")
    if role_identity_stable is not True:
        _fail("UNSTABLE_ROLE_IDENTITY", "role/session/binding identity changed")
    return {
        "ready": True, "project_root": str(expected), "cwd": str(observed_cwd),
        "git_top_level": str(observed_git), "skill_loaded": True,
        "assignment_received": True, "role_identity_stable": True,
    }


def select_role_cli(role_state: Mapping[str, Any], role: str) -> str:
    """Select launchRole.cliTool from the validated target role binding."""
    validated = validate_role_state(role_state)
    if role not in ROLES:
        _fail("INVALID_ROLE", role)
    return str(validated["roles"][ROLES.index(role)]["cli_tool"])


def authorize_role_work(role_state: Mapping[str, Any], role: str, message_generation: int) -> None:
    """Fence ordinary assignment/report work while a migration is in cutover."""
    validated = validate_role_state(role_state)
    if role not in ROLES or message_generation != validated["leader_generation"]:
        _fail("STALE_GENERATION", str(message_generation))
    if validated["migration_phase"] not in (None, "COMMITTED"):
        _fail("ROLE_WORK_FROZEN", str(validated["migration_phase"]))


COMPAT_REQUIRED_DIMENSIONS = (
    "id", "status", "os", "architecture", "runtime_kind", "shell",
    "claude_version", "codex_version", "ccpanes_version_policy", "plugin_version",
    "role_state_schema", "migration_schema", "envelope_schema", "install_sources",
    "cache_action", "upgrade_path", "rollback_path", "version_skew",
    "required_static", "required_capabilities", "required_predicates",
)


def validate_compatibility_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        _fail("INVALID_COMPATIBILITY", "object required")
    _exact(artifact, ("schema_version", "rows"))
    if artifact["schema_version"] != 1 or not isinstance(artifact["rows"], list):
        _fail("UNSUPPORTED_SCHEMA", "compatibility schema must be 1")
    supported_rows: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    for row in artifact["rows"]:
        if not isinstance(row, dict):
            _fail("INVALID_COMPATIBILITY_ROW", "object required")
        _exact(row, COMPAT_REQUIRED_DIMENSIONS)
        if row["status"] not in ("SUPPORTED", "TESTED_ONLY", "UNSUPPORTED"):
            _fail("INVALID_COMPATIBILITY_STATUS", str(row["status"]))
        if row["id"] in ids:
            _fail("DUPLICATE_COMPATIBILITY_ROW", row["id"])
        ids.add(row["id"])
        if row["status"] == "SUPPORTED":
            supported_rows.append(row)
        for key in COMPAT_REQUIRED_DIMENSIONS:
            if key not in ("role_state_schema", "migration_schema", "envelope_schema") and row[key] in (None, "", [], {}):
                _fail("MISSING_COMPATIBILITY_DIMENSION", f"{row['id']}.{key}")
        for key in ("install_sources", "required_capabilities", "required_predicates"):
            if not isinstance(row[key], list) or any(not isinstance(item, str) or not item for item in row[key]) or len(row[key]) != len(set(row[key])):
                _fail("MALFORMED_COMPATIBILITY_DIMENSION", f"{row['id']}.{key}")
        if not isinstance(row["required_static"], dict) or any(not isinstance(key, str) for key in row["required_static"]):
            _fail("MALFORMED_COMPATIBILITY_DIMENSION", f"{row['id']}.required_static")
    if len(supported_rows) != 1 or supported_rows[0]["id"] != "windows-local-claude-2.1.220-codex-0.144.6":
        _fail("INVALID_SUPPORTED_SET", "exactly the normative row must be SUPPORTED")
    supported = supported_rows[0]
    exact = {
        "status": "SUPPORTED", "os": "windows", "architecture": "x64",
        "runtime_kind": "local", "shell": "powershell-5.1",
        "claude_version": "2.1.220", "codex_version": "0.144.6",
        "plugin_version": "0.2.0", "role_state_schema": 1,
        "migration_schema": 1, "envelope_schema": 1, "version_skew": "fail-closed",
    }
    if any(supported.get(key) != value for key, value in exact.items()):
        _fail("NORMATIVE_ROW_MISMATCH", supported["id"])
    if supported["required_capabilities"] != sorted(ADAPTER_OPERATIONS):
        _fail("NORMATIVE_ROW_MISMATCH", "adapter capabilities")
    return copy.deepcopy(artifact)


def compatibility_digest(path: Path) -> str:
    artifact = validate_compatibility_artifact(load_json(path))
    return digest(artifact)


GATE_CONTEXT_FIELDS = (
    "run_id", "migration_id", "source_generation", "target_generation",
    "coordinator_binding_id", "coordinator_session_id", "candidate_binding_id",
    "candidate_session_id", "evidence_started_at", "evidence_expires_at",
)
GATE_IDENTITY_FIELDS = (
    "run_id", "migration_id", "source_generation", "target_generation",
    "owner_binding_id", "owner_session_id", "target_binding_id", "target_session_id",
)
GATE_PROBE_FIELDS = (
    "sequence", "gate_phase", "owner", "primitive", "target", "expected",
    "actual", "cleanup", "probe_id", "operation_key", "identities", "hashes",
    "readback", "compatibility_row_id", "compatibility_digest", "outcome",
    "recorded_at",
)
GATE_PREDICATE_PRIMITIVES = {
    "launch_creates_one_tab_in_existing_pane": "launchRole",
    "session_identity_is_stable": "observeSession",
    "binding_has_exact_candidate_metadata": "bindRole",
    "prompt_output_has_probe_id_nonce_hash": "deliverPrompt",
    "report_observation_has_operation_key_probe_id_hash": "reportToLeader",
    "binding_parent_matches_generation": "reconcile",
    "session_is_stopped": "stopSession",
}
GATE_REQUIRED_PREDICATES = {
    "PRE_LAUNCH": {
        "launch_creates_one_tab_in_existing_pane", "session_identity_is_stable",
    },
    "POST_LAUNCH": {
        "binding_has_exact_candidate_metadata", "prompt_output_has_probe_id_nonce_hash",
        "report_observation_has_operation_key_probe_id_hash", "session_identity_is_stable",
    },
    "PROTECTED_EFFECT": {"binding_parent_matches_generation", "session_is_stopped"},
}


def validate_gate_probe_records(
    records: Any,
    context: Any,
    *,
    compatibility_row_id: str,
    compatibility_artifact_digest: str,
) -> list[dict[str, Any]]:
    """Normalize journaled gate probes and reject uncorrelated assertions."""
    if not isinstance(context, dict):
        _fail("MISSING_GATE_EVIDENCE", "probe_context")
    _exact(context, GATE_CONTEXT_FIELDS)
    for key in ("run_id", "migration_id", "coordinator_binding_id", "coordinator_session_id"):
        _string(context[key], f"probe_context.{key}")
    for key in ("candidate_binding_id", "candidate_session_id"):
        _string(context[key], f"probe_context.{key}", nullable=True)
    if (context["candidate_binding_id"] is None) != (context["candidate_session_id"] is None):
        _fail("INVALID_GATE_CONTEXT", "candidate identity must be wholly present or absent")
    if not isinstance(context["source_generation"], int) or context["target_generation"] != context["source_generation"] + 1:
        _fail("INVALID_GATE_CONTEXT", "generation")
    started = _parse_timestamp(context["evidence_started_at"])
    expires = _parse_timestamp(context["evidence_expires_at"])
    if started >= expires:
        _fail("INVALID_GATE_CONTEXT", "evidence window")
    if not isinstance(records, list) or not records:
        _fail("MISSING_GATE_EVIDENCE", "probe_records")

    normalized: list[dict[str, Any]] = []
    seen_probe_ids: set[str] = set()
    seen_operation_keys: set[str] = set()
    previous_time: dt.datetime | None = None
    previous_phase = -1
    phase_order = {"PRE_LAUNCH": 0, "POST_LAUNCH": 1, "PROTECTED_EFFECT": 2}
    for sequence, source in enumerate(records, 1):
        if not isinstance(source, dict):
            _fail("INVALID_GATE_EVIDENCE", f"record {sequence}")
        _exact(source, GATE_PROBE_FIELDS)
        record = copy.deepcopy(source)
        if record["sequence"] != sequence:
            _fail("REORDERED_GATE_EVIDENCE", f"sequence {sequence}")
        if record["gate_phase"] not in phase_order or phase_order[record["gate_phase"]] < previous_phase:
            _fail("REORDERED_GATE_EVIDENCE", str(record["gate_phase"]))
        previous_phase = phase_order[record["gate_phase"]]
        if record["owner"] not in ("coordinator", "candidate"):
            _fail("INVALID_PROBE_OWNER", str(record["owner"]))
        if record["gate_phase"] in ("PRE_LAUNCH", "PROTECTED_EFFECT") and record["owner"] != "coordinator":
            _fail("INVALID_PROBE_OWNER", record["gate_phase"])
        for key in ("primitive", "probe_id", "operation_key", "compatibility_row_id"):
            _string(record[key], key)
        if record["probe_id"] in seen_probe_ids or record["operation_key"] in seen_operation_keys:
            _fail("DUPLICATE_GATE_EVIDENCE", record["probe_id"])
        seen_probe_ids.add(record["probe_id"])
        seen_operation_keys.add(record["operation_key"])
        if record["operation_key"] != operation_key(context["migration_id"], record["probe_id"], record["primitive"]):
            _fail("GATE_CORRELATION_MISMATCH", "operation_key")

        if not isinstance(record["target"], dict):
            _fail("INVALID_GATE_EVIDENCE", "target")
        _exact(record["target"], ("kind", "id"))
        _string(record["target"]["kind"], "target.kind")
        _string(record["target"]["id"], "target.id")
        if record["expected"] != record["actual"]:
            _fail("GATE_READBACK_MISMATCH", record["probe_id"])

        cleanup = record["cleanup"]
        if not isinstance(cleanup, dict):
            _fail("INVALID_GATE_EVIDENCE", "cleanup")
        _exact(cleanup, ("required", "completed", "evidence_sha256"))
        if not isinstance(cleanup["required"], bool) or cleanup["completed"] is not True:
            _fail("INCOMPLETE_PROBE_CLEANUP", record["probe_id"])
        _string(cleanup["evidence_sha256"], "cleanup.evidence_sha256", nullable=True)
        if cleanup["evidence_sha256"] is not None and not HEX64.fullmatch(cleanup["evidence_sha256"]):
            _fail("INVALID_DIGEST", "cleanup.evidence_sha256")
        if cleanup["required"] and cleanup["evidence_sha256"] is None:
            _fail("INCOMPLETE_PROBE_CLEANUP", record["probe_id"])

        identities = record["identities"]
        if not isinstance(identities, dict):
            _fail("INVALID_GATE_EVIDENCE", "identities")
        _exact(identities, GATE_IDENTITY_FIELDS)
        for key in ("run_id", "migration_id", "owner_binding_id", "owner_session_id"):
            _string(identities[key], f"identities.{key}")
        for key in ("target_binding_id", "target_session_id"):
            _string(identities[key], f"identities.{key}", nullable=True)
        if (identities["target_binding_id"] is None) != (identities["target_session_id"] is None):
            _fail("GATE_CORRELATION_MISMATCH", "partial target identity")
        for key in ("run_id", "migration_id", "source_generation", "target_generation"):
            if identities[key] != context[key]:
                _fail("GATE_CORRELATION_MISMATCH", key)
        owner_prefix = record["owner"]
        if identities["owner_binding_id"] != context[f"{owner_prefix}_binding_id"] or identities["owner_session_id"] != context[f"{owner_prefix}_session_id"]:
            _fail("GATE_CORRELATION_MISMATCH", "owner identity")
        target_pair = (identities["target_binding_id"], identities["target_session_id"])
        coordinator_pair = (context["coordinator_binding_id"], context["coordinator_session_id"])
        candidate_pair = (context["candidate_binding_id"], context["candidate_session_id"])
        target_kind = record["target"]["kind"]
        if target_kind in ("coordinator-binding", "coordinator-session"):
            if target_pair != coordinator_pair:
                _fail("GATE_CORRELATION_MISMATCH", "coordinator target identity")
        elif target_kind in ("candidate-binding", "candidate-session"):
            if candidate_pair == (None, None) or target_pair != candidate_pair:
                _fail("GATE_CORRELATION_MISMATCH", "candidate target identity")
        else:
            _fail("GATE_CORRELATION_MISMATCH", "target kind")

        hashes = record["hashes"]
        if not isinstance(hashes, dict):
            _fail("INVALID_GATE_EVIDENCE", "hashes")
        _exact(hashes, ("expected_sha256", "actual_sha256", "readback_sha256"))
        expected_hashes = {
            "expected_sha256": digest(record["expected"]),
            "actual_sha256": digest(record["actual"]),
        }
        if any(not HEX64.fullmatch(hashes.get(key, "")) or hashes[key] != value for key, value in expected_hashes.items()):
            _fail("GATE_CORRELATION_MISMATCH", "expected/actual hash")

        readback = record["readback"]
        if not isinstance(readback, dict):
            _fail("INVALID_GATE_EVIDENCE", "readback")
        _exact(readback, ("predicate", "outcome", "value"))
        _string(readback["predicate"], "readback.predicate")
        if readback["outcome"] != Observation.COMPLETE.value or readback["value"] != record["actual"]:
            _fail("GATE_READBACK_MISMATCH", record["probe_id"])
        if hashes["readback_sha256"] != digest(readback["value"]):
            _fail("GATE_CORRELATION_MISMATCH", "readback hash")
        expected_primitive = GATE_PREDICATE_PRIMITIVES.get(readback["predicate"])
        if expected_primitive != record["primitive"]:
            _fail("GATE_CORRELATION_MISMATCH", "primitive/predicate")
        if record["compatibility_row_id"] != compatibility_row_id or record["compatibility_digest"] != compatibility_artifact_digest:
            _fail("GATE_CORRELATION_MISMATCH", "compatibility row/digest")
        if record["outcome"] != "SUCCEEDED":
            _fail("PROBE_NOT_SUCCESSFUL", record["probe_id"])
        recorded = _parse_timestamp(record["recorded_at"])
        if recorded < started or recorded >= expires:
            _fail("STALE_GATE_EVIDENCE", record["probe_id"])
        if previous_time is not None and recorded < previous_time:
            _fail("REORDERED_GATE_EVIDENCE", "recorded_at")
        previous_time = recorded
        normalized.append(record)
    return normalized


def gate_compatibility(artifact: Mapping[str, Any], observed: Mapping[str, Any], *, phase: str) -> dict[str, Any]:
    """Fail-closed exact tuple and correlated pre/post capability gate."""
    validated = validate_compatibility_artifact(artifact)
    if phase not in ("PRE_LAUNCH", "POST_LAUNCH", "PROTECTED_MUTATION", "RELEASE"):
        _fail("INVALID_GATE_PHASE", phase)
    dimensions = ("os", "architecture", "runtime_kind", "shell", "claude_version", "codex_version", "plugin_version", "role_state_schema", "migration_schema", "envelope_schema")
    matches = [row for row in validated["rows"] if all(observed.get(key) == row[key] for key in dimensions)]
    if len(matches) != 1:
        _fail("UNSUPPORTED_TUPLE", "unlisted, missing, malformed, or skewed")
    row = matches[0]
    if row["status"] != "SUPPORTED":
        _fail("UNSUPPORTED_TUPLE", row["status"])
    for key in ("install_source", "cache_refreshed", "fresh_sessions"):
        if key not in observed:
            _fail("MISSING_GATE_EVIDENCE", key)
    if observed["install_source"] not in row["install_sources"] or observed["cache_refreshed"] is not True or observed["fresh_sessions"] is not True:
        _fail("STATIC_GATE_FAILED", "install/cache/session")
    for key, expected in row["required_static"].items():
        if observed.get("static", {}).get(key) != expected:
            _fail("STATIC_GATE_FAILED", key)
    capabilities = observed.get("capabilities")
    predicates = observed.get("predicates")
    if not isinstance(capabilities, list) or not isinstance(predicates, list):
        _fail("MISSING_GATE_EVIDENCE", "capabilities/predicates")
    missing = sorted(set(row["required_capabilities"]) - set(capabilities))
    missing_predicates = sorted(set(row["required_predicates"]) - set(predicates))
    if missing or missing_predicates:
        _fail("CAPABILITY_GATE_FAILED", ",".join(missing + missing_predicates))
    if "pre_launch_passed" in observed or "post_launch_passed" in observed:
        _fail("LEGACY_GATE_ASSERTION", "caller-supplied pass booleans are forbidden")
    artifact_sha256 = digest(validated)
    records = validate_gate_probe_records(
        observed.get("probe_records"), observed.get("probe_context"),
        compatibility_row_id=row["id"],
        compatibility_artifact_digest=artifact_sha256,
    )
    coverage = {
        gate_phase: {record["readback"]["predicate"] for record in records if record["gate_phase"] == gate_phase}
        for gate_phase in GATE_REQUIRED_PREDICATES
    }
    required_phases = ["PRE_LAUNCH"]
    if phase in ("POST_LAUNCH", "PROTECTED_MUTATION", "RELEASE"):
        required_phases.append("POST_LAUNCH")
        context = observed["probe_context"]
        if context["candidate_binding_id"] is None:
            _fail("GATE_ORDER_VIOLATION", "post-launch candidate identity is absent")
    if phase == "RELEASE":
        required_phases.append("PROTECTED_EFFECT")
    for required_phase in required_phases:
        missing_evidence = sorted(GATE_REQUIRED_PREDICATES[required_phase] - coverage[required_phase])
        if missing_evidence:
            _fail("MISSING_GATE_EVIDENCE", f"{required_phase}:" + ",".join(missing_evidence))
    evidence_sha256 = digest(records)
    return {
        "allowed": True, "row_id": row["id"], "artifact_digest": artifact_sha256,
        "evidence_digest": evidence_sha256, "phase": phase,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a normative JSON artifact")
    validate.add_argument("kind", choices=("roles", "envelope", "migration", "compatibility", "ledger"))
    validate.add_argument("path", type=Path)
    canon = sub.add_parser("canonicalize", help="print canonical JSON and digest")
    canon.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "canonicalize":
            value = load_json(args.path)
            print(canonical_bytes(value).decode("utf-8"))
            print(digest(value), file=sys.stderr)
        elif args.kind == "roles":
            validate_role_state(load_json(args.path))
        elif args.kind == "envelope":
            validate_envelope(load_json(args.path))
        elif args.kind == "migration":
            validate_migration_journal(load_json(args.path))
        elif args.kind == "compatibility":
            validate_compatibility_artifact(load_json(args.path))
        else:
            read_ledger(args.path, recover_torn_final=False)
    except ProtocolError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

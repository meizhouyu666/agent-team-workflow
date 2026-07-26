#!/usr/bin/env python3
"""Compute a deterministic fingerprint for an agent review snapshot."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ALGORITHM_VERSION = "agent-team-review-fingerprint/v1"
DEFAULT_EXCLUDES = (".codex", ".codex/**")


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git exited with {result.returncode}")
    return result.stdout


def normalize(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def resolve_root(requested: Path) -> Path:
    top_level = git(requested.resolve(), "rev-parse", "--show-toplevel")
    return Path(top_level.decode("utf-8", errors="strict").strip()).resolve()


def pathspecs(scopes: list[str], excludes: list[str]) -> list[str]:
    specs = [normalize(scope) for scope in scopes] or ["."]
    for pattern in excludes:
        normalized = normalize(pattern)
        specs.append(f":(exclude,top,glob){normalized}")
    return specs


def is_excluded(path: str, patterns: list[str]) -> bool:
    normalized = normalize(path)
    return any(
        normalized == normalize(pattern).rstrip("/**")
        or fnmatch.fnmatchcase(normalized, normalize(pattern))
        for pattern in patterns
    )


def add_frame(digest: "hashlib._Hash", label: str, payload: bytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(4, "big"))
    digest.update(label_bytes)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def untracked_content(root: Path, relative_path: str) -> bytes:
    file_path = root / relative_path
    if file_path.is_symlink():
        return ("symlink:" + os.readlink(file_path)).encode("utf-8")
    return file_path.read_bytes()


def compute(
    root: Path,
    scopes: list[str],
    extra_excludes: list[str],
) -> dict[str, object]:
    project_root = resolve_root(root)
    excludes = [*DEFAULT_EXCLUDES, *(normalize(item) for item in extra_excludes)]
    specs = pathspecs(scopes, excludes)

    unstaged = git(
        project_root,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--",
        *specs,
    )
    staged = git(
        project_root,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--",
        *specs,
    )
    untracked_raw = git(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *specs,
    )
    untracked = sorted(
        normalize(item.decode("utf-8", errors="surrogateescape"))
        for item in untracked_raw.split(b"\0")
        if item
    )
    untracked = [item for item in untracked if not is_excluded(item, excludes)]

    digest = hashlib.sha256()
    add_frame(digest, "algorithm", ALGORITHM_VERSION.encode("utf-8"))
    add_frame(digest, "unstaged-diff", unstaged)
    add_frame(digest, "staged-diff", staged)
    for relative_path in untracked:
        add_frame(digest, "untracked-path", relative_path.encode("utf-8"))
        add_frame(
            digest,
            "untracked-content",
            untracked_content(project_root, relative_path),
        )

    return {
        "algorithm": ALGORITHM_VERSION,
        "fingerprint": digest.hexdigest(),
        "root": str(project_root),
        "scope": [normalize(item) for item in scopes] or ["."],
        "excludes": excludes,
        "untracked_files": untracked,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hash tracked, staged, and untracked non-ignored review changes "
            "while excluding coordination files."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Git working tree or worktree root (default: current directory).",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Review-scope path or Git pathspec. Repeat for multiple paths.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional root-relative glob to exclude. Repeat as needed.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print fingerprint metadata as JSON instead of only the digest.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = compute(args.root, args.path, args.exclude)
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"review_fingerprint: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(result["fingerprint"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

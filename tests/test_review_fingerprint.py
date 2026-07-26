from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "agent-team-workflow"
    / "scripts"
    / "review_fingerprint.py"
)
SPEC = importlib.util.spec_from_file_location("review_fingerprint", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class ReviewFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        run(self.repo, "init")
        run(self.repo, "config", "user.name", "Fingerprint Test")
        run(self.repo, "config", "user.email", "fingerprint@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        run(self.repo, "add", "tracked.txt")
        run(self.repo, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fingerprint(self, paths: list[str] | None = None) -> str:
        result = MODULE.compute(self.repo, paths or [], [])
        return str(result["fingerprint"])

    def test_coordination_files_do_not_change_fingerprint(self) -> None:
        (self.repo / "new.txt").write_text("one\n", encoding="utf-8")
        first = self.fingerprint()
        coordination = self.repo / ".codex"
        coordination.mkdir()
        (coordination / "state.md").write_text("volatile\n", encoding="utf-8")
        self.assertEqual(first, self.fingerprint())

    def test_untracked_content_changes_fingerprint(self) -> None:
        target = self.repo / "new.txt"
        target.write_text("one\n", encoding="utf-8")
        first = self.fingerprint()
        target.write_text("two\n", encoding="utf-8")
        self.assertNotEqual(first, self.fingerprint())

    def test_staged_and_unstaged_changes_are_included(self) -> None:
        first = self.fingerprint()
        (self.repo / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
        unstaged = self.fingerprint()
        self.assertNotEqual(first, unstaged)
        run(self.repo, "add", "tracked.txt")
        staged = self.fingerprint()
        self.assertNotEqual(first, staged)
        self.assertNotEqual(unstaged, staged)

    def test_scope_ignores_changes_outside_selected_path(self) -> None:
        (self.repo / "inside").mkdir()
        (self.repo / "outside").mkdir()
        inside = self.repo / "inside" / "a.txt"
        outside = self.repo / "outside" / "b.txt"
        inside.write_text("one\n", encoding="utf-8")
        outside.write_text("one\n", encoding="utf-8")
        first = self.fingerprint(["inside"])
        outside.write_text("two\n", encoding="utf-8")
        self.assertEqual(first, self.fingerprint(["inside"]))
        inside.write_text("two\n", encoding="utf-8")
        self.assertNotEqual(first, self.fingerprint(["inside"]))


if __name__ == "__main__":
    unittest.main()

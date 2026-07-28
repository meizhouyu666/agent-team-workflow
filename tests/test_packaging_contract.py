from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-team-workflow"
VERSION = "0.2.0"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def frontmatter_keys(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError(f"{path} has no YAML frontmatter")
    keys: set[str] = set()
    for line in lines[1:]:
        if line == "---":
            return keys
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if match:
            keys.add(match.group(1))
    raise AssertionError(f"{path} has unterminated YAML frontmatter")


class PackagingContractTests(unittest.TestCase):
    def test_native_manifests_are_independent_and_version_aligned(self) -> None:
        codex = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
        claude = load_json(PLUGIN / ".claude-plugin" / "plugin.json")
        self.assertEqual(VERSION, codex.get("version"))
        self.assertEqual(VERSION, claude.get("version"))
        self.assertEqual("agent-team-workflow", codex.get("name"))
        self.assertEqual("agent-team-workflow", claude.get("name"))

    def test_marketplaces_source_same_plugin_and_declare_version(self) -> None:
        for relative in (
            Path(".agents/plugins/marketplace.json"),
            Path(".claude-plugin/marketplace.json"),
        ):
            marketplace = load_json(ROOT / relative)
            entries = marketplace.get("plugins")
            self.assertIsInstance(entries, list)
            self.assertEqual(1, len(entries))
            entry = entries[0]
            self.assertEqual("agent-team-workflow", entry.get("name"))
            self.assertEqual(VERSION, entry.get("version"))
            source_text = json.dumps(entry.get("source"), sort_keys=True)
            self.assertIn("plugins/agent-team-workflow", source_text.replace("\\", "/"))

    def test_shared_skill_frontmatter_is_portable(self) -> None:
        skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
        self.assertEqual(3, len(skills))
        for skill in skills:
            self.assertEqual({"name", "description"}, frontmatter_keys(skill))

    def test_runtime_state_is_intentionally_ignored(self) -> None:
        patterns = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue({".ccpanes/", ".ccpanes/**"} & patterns)

    def test_operator_docs_name_supported_topology_and_release(self) -> None:
        english = (ROOT / "README_EN.md").read_text(encoding="utf-8")
        protocol = (ROOT / "docs/protocol.md").read_text(encoding="utf-8")
        adapter = (ROOT / "docs/adapters/codex-ccpanes.md").read_text(
            encoding="utf-8"
        )
        combined = "\n".join((english, protocol, adapter))
        for required in (
            "0.2.0",
            "Claude",
            "Codex",
            "SUPPORTED",
            "TESTED_ONLY",
            "UNSUPPORTED",
            "--plugin-dir",
            "STATE_ACTIVATED",
            "INDETERMINATE",
        ):
            self.assertIn(required, combined)

    def test_v9_authority_phase_order_matches_schemas_docs_and_leader_skill(self) -> None:
        expected = [
            "CREATED", "PREFLIGHTED", "PREPARED", "VALIDATED",
            "NEW_LEADER_REGISTERED", "EXECUTOR_REPARENTED", "REVIEWER_REPARENTED",
            "STATE_ACTIVATED", "WORKERS_ACKED", "OLD_SUPERSEDED", "COMMITTED",
            "ABORTED", "ROLLED_BACK",
        ]
        migration = load_json(PLUGIN / "schemas" / "migration.schema.json")
        roles = load_json(PLUGIN / "schemas" / "role-state.schema.json")
        self.assertEqual(expected, migration["properties"]["phase"]["enum"])
        self.assertEqual([None, *expected], roles["properties"]["migration_phase"]["enum"])
        protocol = (ROOT / "docs/protocol.md").read_text(encoding="utf-8")
        skill = (PLUGIN / "skills" / "lead-agent-workflow" / "SKILL.md").read_text(encoding="utf-8")
        for text in (protocol, skill):
            self.assertLess(text.index("`WORKERS_ACKED`"), text.index("`OLD_SUPERSEDED`"))
            self.assertLess(text.index("`OLD_SUPERSEDED`"), text.index("`COMMITTED`"))

    def test_lean_mode_and_review_budget_are_consistent(self) -> None:
        skills = [
            (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            for name in (
                "lead-agent-workflow", "orchestrate-agent-team", "review-agent-work",
            )
        ]
        for text in skills:
            for required in (
                r"`lean`", r"`assurance`", r"one\s+implementation review",
                r"at most one focused re-review", r"cannot widen accepted scope",
                r"A P2 does not block", r"approved acceptance criterion",
                r"`spec_version`", r"full E2E",
            ):
                self.assertRegex(text, required)

        combined_docs = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README_EN.md", "docs/protocol.md", "docs/operator-guide.md",
            )
        )
        for required in (
            r"`lean` is the default", r"`assurance`", r"one\s+implementation review",
            r"at most one", r"P2", r"acceptance violation", r"`spec_version`",
            r"full E2E", r"takeover remains deferred",
        ):
            self.assertRegex(combined_docs, required)

    def test_codex_topology_is_default_and_install_is_inert(self) -> None:
        skill_texts = [
            (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            for name in (
                "lead-agent-workflow", "orchestrate-agent-team", "review-agent-work",
            )
        ]
        for text in skill_texts:
            self.assertIn("`codex-three-pane`", text)
            self.assertIn("`claude-leader`", text)
            self.assertRegex(text, r"explicit(?:ly)?(?: user-approved| approved|\s+approved)")
            self.assertRegex(text, r"Installation, upgrade")
            self.assertIn("`.codex/roles.json`", text)

        docs = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README_EN.md", "docs/protocol.md", "docs/operator-guide.md",
                "docs/adapters/codex-ccpanes.md",
            )
        )
        for required in (
            "codex-three-pane", "claude-leader", "Codex Leader",
            "Installation and upgrade are inert", "does not select a topology",
        ):
            self.assertIn(required, docs)
        self.assertRegex(docs, r"does not\s+create `\.codex/roles\.json`")

        codex_manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
        claude_manifest = load_json(PLUGIN / ".claude-plugin" / "plugin.json")
        codex_market = load_json(ROOT / ".agents/plugins/marketplace.json")["plugins"][0]
        claude_market = load_json(ROOT / ".claude-plugin/marketplace.json")["plugins"][0]
        self.assertIn("three-Codex", codex_manifest["description"])
        self.assertIn("explicit", codex_manifest["description"])
        self.assertIn("optional Claude Leader", claude_manifest["description"])
        self.assertIn("Default Codex Leader", codex_market["description"])
        self.assertIn("explicit", claude_market["description"])

        role_ui = {
            name: (PLUGIN / "skills" / name / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
            for name in (
                "lead-agent-workflow", "orchestrate-agent-team", "review-agent-work",
            )
        }
        self.assertIn("codex-three-pane", role_ui["lead-agent-workflow"])
        self.assertIn("explicitly approved claude-leader", role_ui["lead-agent-workflow"])
        for text in role_ui.values():
            self.assertNotIn("Claude-led team", text)

        compatibility = load_json(PLUGIN / "compatibility.json")["rows"][0]
        self.assertEqual(
            "manual-refresh-only-install-and-load-are-inert",
            compatibility["cache_action"],
        )
        self.assertEqual(
            "preserve-codex-three-pane-schema-0-unless-claude-leader-explicitly-approved",
            compatibility["upgrade_path"],
        )

    def test_lean_behavior_guard_contract_is_consistent(self) -> None:
        script = PLUGIN / "scripts" / "workflow_guard.py"
        self.assertTrue(script.is_file())
        skills = {
            name: (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            for name in (
                "lead-agent-workflow", "orchestrate-agent-team", "review-agent-work",
            )
        }
        shared = (
            ".codex/guards/<run-id>.json",
            "`spec_revisions: 1`",
            "`internal_agents: 0`",
            "`implementation_reviews: 1`",
            "`focused_re_reviews: 1`",
            "`same_failure_retries: 2`",
            "`full_test_suite_runs: 1`",
            "`scope_expansions: 0`",
            "SPEC_LIMIT_REACHED",
            "AGENT_LIMIT_REACHED",
            "REVIEW_LIMIT_REACHED",
            "RETRY_LIMIT_REACHED",
            "TEST_LIMIT_REACHED",
            "SCOPE_APPROVAL_REQUIRED",
            "not a security boundary",
        )
        for text in skills.values():
            for required in shared:
                self.assertIn(required, text)

        for action in (
            "`spec_revision`", "`scope_expansion`", "`implementation_review`",
            "`focused_re_review`",
        ):
            self.assertIn(action, skills["lead-agent-workflow"])
        for action in ("`internal_agent`", "`same_failure_retry`", "`full_test_suite`"):
            self.assertIn(action, skills["orchestrate-agent-team"])
        self.assertIn("matching consumed", skills["review-agent-work"])
        self.assertIn("never widens", skills["review-agent-work"])

        docs = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README_EN.md", "docs/protocol.md", "docs/operator-guide.md",
            )
        )
        for required in (
            "workflow_guard.py", "init", "consume", "status",
            ".codex/guards/<run-id>.json", "stable operation",
            "failure key", "atomically", "SCOPE_APPROVAL_REQUIRED",
            "not a security boundary",
        ):
            self.assertIn(required, docs)


if __name__ == "__main__":
    unittest.main()

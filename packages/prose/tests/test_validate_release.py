"""Prose-specific release checks."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_release  # noqa: E402


class ValidateReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.clone_index = 0

    def clone_source(self, name: str = "copy") -> Path:
        self.clone_index += 1
        destination = self.parent / f"{name}-{self.clone_index}"
        shutil.copytree(ROOT / "plugin", destination, symlinks=True)
        return destination

    @staticmethod
    def codes(root: Path, installed: Path | None = None) -> set[str]:
        return {finding.code for finding in validate_release.validate(root, installed)}

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def read_csv(path: Path) -> list[dict[str, str]]:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def write_csv(
        path: Path, rows: list[dict[str, str]], fields: list[str] | None = None
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=list(rows[0]) if fields is None else fields,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_missing_terminology_source_join_is_rejected(self) -> None:
        root = self.clone_source()
        path = root / "skills/prose/references/terminology-records.csv"
        rows = self.read_csv(path)
        rows[0]["source_id"] = "AUTH-MISSING"
        self.write_csv(path, rows)
        self.assertIn("terminology-source-missing", self.codes(root))

    def test_duplicate_terminology_id_is_rejected(self) -> None:
        root = self.clone_source()
        path = root / "skills/prose/references/terminology-records.csv"
        rows = self.read_csv(path)
        rows.append(rows[0])
        self.write_csv(path, rows)
        self.assertIn("terminology-register-invalid", self.codes(root))

    def test_malformed_register_headers_are_rejected(self) -> None:
        for problem in ("missing", "duplicate", "extra"):
            with self.subTest(problem=problem):
                root = self.clone_source()
                path = root / "skills/prose/references/terminology-records.csv"
                rows = self.read_csv(path)
                fields = list(rows[0])
                fields = (
                    fields[1:]
                    if problem == "missing"
                    else [
                        *fields,
                        fields[0] if problem == "duplicate" else "unsupported",
                    ]
                )
                self.write_csv(path, rows, fields)
                self.assertIn("terminology-register-invalid", self.codes(root))

    def test_csv_column_order_and_quoting_do_not_change_meaning(self) -> None:
        root = self.clone_source()
        for path in (root / "skills/prose/references").glob("*.csv"):
            rows = self.read_csv(path)
            self.write_csv(path, rows, list(reversed(rows[0])))
        self.assertEqual(self.codes(root), set())

    def test_missing_google_decision_join_is_rejected(self) -> None:
        root = self.clone_source()
        path = (
            root
            / "skills/prose/references/google-developer-documentation-word-list-entries.csv"
        )
        rows = self.read_csv(path)
        rows[0]["decision_ids"] = "GWL-99999999"
        self.write_csv(path, rows)
        self.assertIn("google-register-invalid", self.codes(root))

    def test_google_decision_destinations_accept_files_headings_and_authorities(
        self,
    ) -> None:
        root = self.clone_source()
        references = root / "skills/prose/references"
        (references / "destination.md").write_text(
            "# Destination heading\n\n## Ændringer\n", encoding="utf-8"
        )
        path = references / "google-developer-documentation-word-list-decisions.csv"
        rows = self.read_csv(path)
        rows[0]["destinations"] = (
            "references/destination.md;"
            "references/destination.md#destination-heading;"
            "references/destination.md#ændringer;"
            "host-or-project-authority"
        )
        self.write_csv(path, rows)
        self.assertNotIn("google-register-invalid", self.codes(root))

    def test_google_decision_destinations_reject_invalid_paths_and_headings(
        self,
    ) -> None:
        root = self.clone_source()
        references = root / "skills/prose/references"
        (references / "destination.md").write_text(
            "# Destination heading\n", encoding="utf-8"
        )
        path = references / "google-developer-documentation-word-list-decisions.csv"
        rows = self.read_csv(path)
        for destination in (
            "references/destination.md#missing-heading",
            "references/destination.md#",
            "references/destination.md#destination-heading#extra",
            "references/missing.md",
            "references/missing.md#destination-heading",
            "references/../references/destination.md#destination-heading",
            "references//destination.md#destination-heading",
            "./references/destination.md#destination-heading",
            "/references/destination.md#destination-heading",
            "references\\destination.md#destination-heading",
            "references/destination.md?query#destination-heading",
            "references/%64estination.md#destination-heading",
            "references/destination.md#destination%2Dheading",
            "https://example.com/destination.md#destination-heading",
        ):
            with self.subTest(destination=destination):
                rows[0]["destinations"] = destination
                self.write_csv(path, rows)
                self.assertIn("google-register-invalid", self.codes(root))

    def test_research_rationale_references_must_resolve(self) -> None:
        root = self.clone_source()
        path = root / "skills/prose/references/source-evidence/research-corpus.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\nRationale cites `AUTH-AUDIT-MISSING`.\n",
            encoding="utf-8",
        )
        self.assertIn("research-source-missing", self.codes(root))

    def test_generated_cache_is_rejected(self) -> None:
        root = self.clone_source()
        cache = root / "__pycache__"
        cache.mkdir()
        (cache / "generated.pyc").write_bytes(b"cache")
        self.assertIn("generated-cache-present", self.codes(root))

    def test_symlink_is_rejected(self) -> None:
        root = self.clone_source()
        outside = self.parent / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        (root / "skills/prose/outside.md").symlink_to(outside)
        self.assertIn("symlink-forbidden", self.codes(root))

    def test_default_host_hook_configuration_is_rejected(self) -> None:
        for relative in ("hooks/hooks.json", "Hooks/Hooks.json"):
            with self.subTest(path=relative):
                root = self.clone_source()
                hook = root / relative
                hook.parent.mkdir()
                self.write_json(hook, {"hooks": {"SessionStart": []}})
                self.assertIn("hook-artifact-present", self.codes(root))

    def test_manifest_hook_field_is_rejected(self) -> None:
        root = self.clone_source()
        manifest = root / ".codex-plugin/plugin.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["hooks"] = "./hooks.json"
        self.write_json(manifest, value)
        self.assertIn("manifest-hook-forbidden", self.codes(root))

    def test_extra_skill_directory_is_rejected(self) -> None:
        root = self.clone_source()
        (root / "skills/extra").mkdir()
        self.assertIn("unexpected-skill-directory", self.codes(root))

    def test_missing_skill_directory_is_rejected(self) -> None:
        root = self.clone_source()
        shutil.rmtree(root / "skills/prose-maintainer")
        self.assertIn("required-skill-directory-missing", self.codes(root))

    def test_installed_payload_drift_is_rejected(self) -> None:
        source = self.clone_source("source")
        installed = self.clone_source("installed")
        path = installed / "skills/prose/SKILL.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nDrift.\n", encoding="utf-8"
        )
        self.assertIn("payload-digest-mismatch", self.codes(source, installed))

    def test_installed_file_set_must_match_source(self) -> None:
        for change in ("missing", "extra"):
            with self.subTest(change=change):
                source = self.clone_source("source")
                installed = self.clone_source("installed")
                if change == "missing":
                    (installed / "skills/prose/agents/openai.yaml").unlink()
                else:
                    (installed / "extra.txt").write_text(
                        "Extra payload.\n", encoding="utf-8"
                    )
                self.assertIn(
                    "payload-file-set-mismatch", self.codes(source, installed)
                )

    def test_installed_vcs_metadata_is_rejected(self) -> None:
        source = self.clone_source("source")
        installed = self.clone_source("installed")
        git_directory = installed / ".git"
        git_directory.mkdir()
        (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.assertIn("git-residue", self.codes(source, installed))

    def test_exact_installed_payload_passes(self) -> None:
        source = self.clone_source("source")
        installed = self.clone_source("installed")
        self.assertEqual(validate_release.validate(source, installed), [])

    def test_missing_source_evidence_file_is_rejected(self) -> None:
        root = self.clone_source()
        register = root / "skills/prose/references/source-authorities.csv"
        rows = self.read_csv(register)
        rows[0]["evidence_record"] = "source-evidence/missing.md#missing"
        self.write_csv(register, rows)
        self.assertIn("source-evidence-record-missing", self.codes(root))

    def test_missing_source_evidence_anchor_is_rejected(self) -> None:
        root = self.clone_source()
        register = root / "skills/prose/references/source-authorities.csv"
        rows = self.read_csv(register)
        source = rows[0]["evidence_record"].split("#", 1)[0]
        rows[0]["evidence_record"] = f"{source}#missing-heading"
        self.write_csv(register, rows)
        self.assertIn("source-evidence-anchor-missing", self.codes(root))

    def test_cli_json_is_structured(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/validate_release.py"),
                "--source",
                str(ROOT / "plugin"),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            payload, {"schema_version": 1, "checks_passed": True, "findings": []}
        )

    def test_local_links_resolve_targets_and_heading_anchors(self) -> None:
        root = self.clone_source()
        directory = root / "skills/prose/references"
        target = directory / "linked target.md"
        target.write_text(
            "# A *styled* `heading`\n\n"
            "Setext title\n============\n\n"
            "# Repeated\n\n# Repeated\n\n# Straße og é\n",
            encoding="utf-8",
        )
        links = directory / "link-example.md"
        links.write_text(
            "[Inline](linked%20target.md#a-styled-heading)\n\n"
            "[Reference][target]\n\n"
            "[target]: linked%20target.md#setext-title\n\n"
            "[Repeated](linked%20target.md#repeated-1)\n\n"
            "[Unicode](linked%20target.md#straße-og-é)\n\n"
            "`[Literal](absent.md)`\n\n"
            "```markdown\n[Literal](absent.md)\n```\n",
            encoding="utf-8",
        )
        evidence = root / "skills/prose/references/source-evidence/unicode.md"
        evidence.write_text("# Straße og é\n", encoding="utf-8")
        register = root / "skills/prose/references/source-authorities.csv"
        rows = self.read_csv(register)
        rows[0]["evidence_record"] = "source-evidence/unicode.md#straße-og-é"
        self.write_csv(register, rows)
        self.assertEqual(self.codes(root), set())
        target.write_text("# Different heading\n", encoding="utf-8")
        self.assertIn("markdown-anchor-missing", self.codes(root))
        target.unlink()
        self.assertIn("markdown-link-missing", self.codes(root))

    def test_only_rendered_headings_supply_anchors(self) -> None:
        root = self.clone_source()
        path = root / "skills/prose/references/link-example.md"
        path.write_text(
            "[Heading](#example)\n\n```markdown\n# Example\n```\n",
            encoding="utf-8",
        )
        self.assertIn("markdown-anchor-missing", self.codes(root))

    def test_images_and_unsafe_local_links_are_checked(self) -> None:
        for markup, expected in (
            ("![Missing image](absent.svg)\n", "markdown-link-missing"),
            ("[Outside](../../../../outside.md)\n", "markdown-link-invalid"),
        ):
            with self.subTest(markup=markup):
                root = self.clone_source()
                (root / "skills/prose/references/link-example.md").write_text(
                    markup, encoding="utf-8"
                )
                self.assertIn(expected, self.codes(root))

    def test_malformed_manifest_is_rejected_by_public_cli(self) -> None:
        for contents in ('{"name":', "[]", '{"name":"prose","name":"other"}'):
            with self.subTest(contents=contents):
                root = self.clone_source()
                relative = ".codex-plugin/plugin.json"
                (root / relative).write_text(contents, encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(ROOT / "scripts/validate_release.py"),
                        "--source",
                        str(root),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                payload = json.loads(completed.stdout)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(payload["checks_passed"])
                self.assertTrue(
                    any(
                        finding["path"] == relative and finding["message"]
                        for finding in payload["findings"]
                    )
                )


if __name__ == "__main__":
    unittest.main()

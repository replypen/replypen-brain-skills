from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_structure.py"
SPEC = importlib.util.spec_from_file_location("brain_structure", SCRIPT)
assert SPEC and SPEC.loader
bs = importlib.util.module_from_spec(SPEC)
# Register before exec: `from __future__ import annotations` makes dataclasses resolve string
# annotations via sys.modules[module].__dict__ at class-creation time.
sys.modules[SPEC.name] = bs
SPEC.loader.exec_module(bs)


def git(root: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")


def init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str = "snapshot") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def lint_stub(parent: Path, exit_code: int = 0) -> Path:
    stub = parent / "lint_stub.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path('lint_calls.jsonl').open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8")
    return stub


def good_brain(root: Path) -> None:
    """A minimal valid brain: router linking a routed note and a skill; both resolve; note reachable."""
    write(root, "AGENTS.md",
          "# Router\n\nSee [intake notes](notes/intake.md) and [demo skill](skills/demo/SKILL.md).\n")
    write(root, "notes/intake.md", "# Intake\n\nDistilled intake handling. No raw mail here.\n")
    write(root, "skills/demo/SKILL.md",
          "---\nname: demo\ndescription: A demo skill for the fixture.\n---\n\n# Demo\n\nBody.\n")


def run_main(root: Path, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bs.main(["--root", str(root), *extra])
    return code, out.getvalue()


class BrainStructureTests(unittest.TestCase):
    def test_passing_brain_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            commit_all(root)
            code, output = run_main(root, "--lint-script", str(lint_stub(Path(tmp))))
            self.assertEqual(code, 0, output)
            self.assertIn("failed=0", output)
            self.assertIn("failed_checks=-", output)

    def test_broken_relative_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, "AGENTS.md",
                  "# Router\n\nSee [intake](notes/intake.md) and [gone](notes/missing.md).\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint")
            self.assertEqual(code, 1, output)
            self.assertIn("links: link target does not resolve", output)
            self.assertIn("notes/missing.md", output)
            self.assertIn("failed_checks=links", output)

    def test_invalid_or_missing_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            # No front-matter fence at all.
            write(root, "skills/broken/SKILL.md", "# Broken\n\nNo front matter.\n")
            # Fence present but description empty.
            write(root, "skills/thin/SKILL.md", "---\nname: thin\ndescription:\n---\n\n# Thin\n")
            # Link them so the links check stays green.
            write(root, "AGENTS.md",
                  "# Router\n\n[intake](notes/intake.md) [demo](skills/demo/SKILL.md) "
                  "[a](skills/broken/SKILL.md) [b](skills/thin/SKILL.md)\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint")
            self.assertEqual(code, 1, output)
            self.assertIn("skills/broken/SKILL.md:1: frontmatter: missing or unterminated", output)
            self.assertIn("skills/thin/SKILL.md:1: frontmatter: front-matter is missing a non-empty "
                          "'description'", output)

    def test_orphaned_case_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, "notes/orphan.md", "# Orphan\n\nNothing routes here.\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint")
            self.assertEqual(code, 1, output)
            self.assertIn("notes/orphan.md: reachability: routed file is not reachable", output)
            # The linked note must not be reported as orphaned.
            self.assertNotIn("notes/intake.md: reachability", output)

    def test_ignored_path_references_are_nonblocking_notices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, ".replypenignore", "/tests/\n/_internal/\n")
            write(root, "tests/fixture.json", "{}\n")
            write(root, "_internal/operator.md",
                  "Maintainer note may cite tests/fixture.json and _internal/operator.md.\n")
            write(root, "skills/demo/scripts/check.py",
                  '"""See tests/fixture.json and _internal/operator.md for details."""\n')
            write(root, "AGENTS.md",
                  "# Router\n\n[intake](notes/intake.md) [demo](skills/demo/SKILL.md)\n\n"
                  "Open `.gitignore` before debugging.\n")
            commit_all(root)

            code, output = run_main(root, "--lint-script", str(lint_stub(Path(tmp))))

            self.assertEqual(code, 0, output)
            self.assertIn("NOTICE skills/demo/scripts/check.py:1: ignored-refs:", output)
            self.assertIn("hidden path unavailable in runs: tests/fixture.json", output)
            self.assertIn("hidden path unavailable in runs: _internal/", output)
            self.assertIn("NOTICE AGENTS.md:5: ignored-refs:", output)
            self.assertIn("hidden path unavailable in runs: .gitignore", output)
            self.assertNotIn("NOTICE _internal/operator.md", output)
            self.assertIn("failed=0", output)
            self.assertIn("notices=3", output)

    def test_control_mechanism_prose_and_control_files_do_not_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, ".replypenignore", "/tests/\n.replypenignore\n")
            write(root, "tests/fixture.json", "{}\n")
            write(root, "notes/intake.md",
                  "# Intake\n\nThe `.replypenignore` mechanism hides maintainer-only tests.\n")
            commit_all(root)

            code, output = run_main(root, "--lint-script", str(lint_stub(Path(tmp))))

            self.assertEqual(code, 0, output)
            self.assertIn("notices=0", output)
            self.assertNotIn("NOTICE", output)

    def test_root_ignored_target_and_union_of_ignore_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, ".replypenignore", "/operator-notes.md\n/shared.md\n!/shared.md\n")
            write(root, ".rcignore", "/shared.md\n")
            write(root, "operator-notes.md", "maintainer only\n")
            write(root, "shared.md", "hidden by the other control\n")
            write(root, "AGENTS.md",
                  "# Router\n\n[intake](notes/intake.md) [demo](skills/demo/SKILL.md)\n\n"
                  "Read `operator-notes.md` and [shared details](shared.md).\n")
            commit_all(root)

            code, output = run_main(root, "--lint-script", str(lint_stub(Path(tmp))))

            self.assertEqual(code, 0, output)
            self.assertIn("hidden path unavailable in runs: operator-notes.md", output)
            self.assertIn("hidden path unavailable in runs: shared.md", output)
            self.assertIn("notices=2", output)

    def test_table_router_with_backticked_paths_and_harvest_records_are_reachable(self):
        # The documented router convention is a symptom -> path table with backticked paths, not
        # markdown links; committed harvest records are audit artifacts needing no router path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, "AGENTS.md",
                  "# Router\n\n| symptom | where |\n|---|---|\n"
                  "| refunds | `playbooks/refund.md` |\n| terms | `terminology.md` |\n"
                  "| intake | `notes/intake.md` |\n")
            write(root, "playbooks/refund.md", "# Refunds\n\nDistilled playbook.\n")
            write(root, "terminology.md", "# Terms\n\nGlossary.\n")
            write(root, "notes/harvest-records/2026-07-19.md",
                  "# Harvest record\n\nCounts and scores only.\n")
            commit_all(root)
            code, output = run_main(root, "--lint-script", str(lint_stub(Path(tmp))))
            self.assertEqual(code, 0, output)
            self.assertNotIn("reachability", output.split("SUMMARY")[0])

    def test_full_tree_lint_is_strict_only_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            commit_all(root)
            stub = str(lint_stub(Path(tmp)))
            calls_log = root / "lint_calls.jsonl"

            code, output = run_main(root, "--lint-script", stub)
            self.assertEqual(code, 0, output)
            calls = [json.loads(ln) for ln in calls_log.read_text().splitlines()]
            self.assertEqual(calls, [[], ["--all"]], "default full-tree pass must not be strict")

            calls_log.unlink()
            code, output = run_main(root, "--lint-script", stub, "--strict-lint")
            self.assertEqual(code, 0, output)
            calls = [json.loads(ln) for ln in calls_log.read_text().splitlines()]
            self.assertEqual(calls, [[], ["--all", "--strict"]])

    def test_tracked_raw_harvest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, ".replypen/exports/e1/threads/2024-01--refund--3.md", "raw thread text\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint")
            self.assertEqual(code, 1, output)
            self.assertIn("raw-tracked: raw-harvest path is tracked", output)
            self.assertIn("2024-01--refund--3.md", output)

    def test_historical_raw_harvest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            # Split-shaped file committed on a normal (non-ignored) path, then removed from the tree.
            write(root, "imported/2019-05--notary-question--1.md", "raw thread text\n")
            commit_all(root, "add raw")
            git(root, "rm", "-q", "imported/2019-05--notary-question--1.md")
            commit_all(root, "remove raw")
            code, output = run_main(root, "--skip", "lint")
            self.assertEqual(code, 1, output)
            self.assertIn("raw-history: raw-harvest path exists in git history", output)
            self.assertIn("2019-05--notary-question--1.md", output)
            self.assertIn("history rewrite", output)
            # It is gone from the tree, so raw-tracked stays clean.
            self.assertNotIn("raw-tracked:", output)

    def test_leftover_scratch_root_with_expect_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            commit_all(root)
            (root / ".replypen" / "harvest").mkdir(parents=True)
            stub = str(lint_stub(Path(tmp)))
            # Without --expect-clean the scratch check does not run.
            code_default, out_default = run_main(root, "--lint-script", stub)
            self.assertEqual(code_default, 0, out_default)
            # With --expect-clean it fails.
            code, output = run_main(root, "--lint-script", stub, "--expect-clean")
            self.assertEqual(code, 1, output)
            self.assertIn("scratch: sensitive harvest scratch root still exists", output)
            self.assertIn("failed_checks=scratch", output)

    def test_lint_subprocess_failure_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            commit_all(root)
            failing = str(lint_stub(Path(tmp), exit_code=1))
            code, output = run_main(root, "--lint-script", failing)
            self.assertEqual(code, 1, output)
            self.assertIn("lint: brain_lint staged mode failed (exit 1)", output)
            self.assertIn("lint: brain_lint full-tree mode failed (exit 1)", output)

    def test_missing_lint_script_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            commit_all(root)
            code, output = run_main(root, "--lint-script", str(Path(tmp) / "nope.py"))
            self.assertEqual(code, 1, output)
            self.assertIn("lint: privacy/contract lint script not found", output)

    def test_json_output_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, "AGENTS.md",
                  "# Router\n\n[intake](notes/intake.md) [demo](skills/demo/SKILL.md) [x](notes/gone.md)\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint", "--json")
            self.assertEqual(code, 1, output)
            report = json.loads(output)
            self.assertFalse(report["ok"])
            self.assertIsInstance(report["checks"], list)
            names = {c["name"] for c in report["checks"]}
            self.assertLessEqual({"links", "frontmatter", "reachability", "lint", "raw-tracked",
                                  "raw-history"}, names)
            lint_check = next(c for c in report["checks"] if c["name"] == "lint")
            self.assertTrue(lint_check["skipped"])
            summary = report["summary"]
            self.assertEqual(set(summary), {"checks", "ran", "passed", "failed", "findings",
                                            "notices", "baselined", "failed_checks"})
            self.assertIn("links", summary["failed_checks"])
            self.assertTrue(all(set(f) == {"check", "path", "line", "message", "severity"}
                                for f in report["findings"]))
            self.assertTrue(any(f["check"] == "links" for f in report["findings"]))

    def test_run_hidden_source_links_are_not_judged(self):
        """`.agents/` & co are absent from every run; their links may point at gitignored kit skills."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            init_repo(root)
            good_brain(root)
            write(root, ".replypenignore", "/.agents/\n")
            write(root, ".agents/skills/local/SKILL.md",
                  "---\nname: local\ndescription: Maintainer-only skill.\n---\n\n"
                  "See [rp-debug](../rp-debug/SKILL.md); run `rp dev console --project x`.\n")
            commit_all(root)
            code, output = run_main(root, "--skip", "lint", "--scope", "full")
            self.assertEqual(code, 0, output)
            self.assertNotIn("rp-debug", output)
            self.assertIn("failed_checks=-", output)


if __name__ == "__main__":
    unittest.main()

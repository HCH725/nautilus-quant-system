from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_plan_ref.py"
SPEC = spec_from_file_location("verify_plan_ref", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify_plan_ref = module_from_spec(SPEC)
SPEC.loader.exec_module(verify_plan_ref)


class PlanRefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="plan-ref-proof-")
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Plan Test")
        self.git("config", "user.email", "plan-test@example.invalid")
        plan = self.repo / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text(
            "# Plan\n\n"
            "<!-- PLAN_ID:PLAN-V1 -->\n"
            "<!-- PLAN_SECTION:STAGE-0 -->\n"
            "## Stage 0\n",
            encoding="utf-8",
        )
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "plan")
        self.commit = self.output("rev-parse", "HEAD")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(
            ("git", *args),
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def output(self, *args):
        return subprocess.check_output(("git", *args), cwd=self.repo, text=True).strip()

    def verify(self, **overrides):
        values = {
            "repo": self.repo,
            "commit": self.commit,
            "path": "docs/plan.md",
            "plan_id": "PLAN-V1",
            "sections": ["STAGE-0"],
        }
        values.update(overrides)
        return verify_plan_ref.verify_plan_ref(**values)

    def test_accepts_exact_ancestor_commit_path_plan_and_section(self):
        self.assertEqual("PASS", self.verify()["status"])

    def test_rejects_abbreviated_commit(self):
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "full 40-character"):
            self.verify(commit=self.commit[:12])

    def test_rejects_path_traversal(self):
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "repository-relative"):
            self.verify(path="../plan.md")

    def test_rejects_duplicate_path_separator(self):
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "repository-relative"):
            self.verify(path="docs//plan.md")

    def test_rejects_missing_section(self):
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "missing or not unique"):
            self.verify(sections=["STAGE-1"])

    def test_rejects_wrong_plan_id(self):
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "plan_id marker"):
            self.verify(plan_id="PLAN-V2")

    def test_rejects_duplicate_section_marker(self):
        plan = self.repo / "docs" / "plan.md"
        plan.write_text(plan.read_text() + "<!-- PLAN_SECTION:STAGE-0 -->\n")
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "duplicate marker")
        duplicate_commit = self.output("rev-parse", "HEAD")
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "missing or not unique"):
            self.verify(commit=duplicate_commit)

    def test_rejects_section_marker_hidden_in_code_fence(self):
        plan = self.repo / "docs" / "plan.md"
        plan.write_text(
            plan.read_text().replace(
                "<!-- PLAN_SECTION:STAGE-0 -->\n",
                "```text\n<!-- PLAN_SECTION:STAGE-0 -->\n```\n",
            )
        )
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "hide marker in code fence")
        fenced_commit = self.output("rev-parse", "HEAD")
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "missing or not unique"):
            self.verify(commit=fenced_commit)

    def test_rejects_section_marker_embedded_in_prose(self):
        plan = self.repo / "docs" / "plan.md"
        plan.write_text(
            plan.read_text().replace(
                "<!-- PLAN_SECTION:STAGE-0 -->",
                "prefix <!-- PLAN_SECTION:STAGE-0 --> suffix",
            )
        )
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "embed marker in prose")
        inline_commit = self.output("rev-parse", "HEAD")
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "missing or not unique"):
            self.verify(commit=inline_commit)

    def test_rejects_commit_that_is_not_ancestor_of_head(self):
        self.git("switch", "--detach", self.commit)
        other = self.repo / "other.txt"
        other.write_text("side\n")
        self.git("add", "other.txt")
        self.git("commit", "-m", "side")
        side_commit = self.output("rev-parse", "HEAD")
        self.git("switch", "main")
        main = self.repo / "main.txt"
        main.write_text("main\n")
        self.git("add", "main.txt")
        self.git("commit", "-m", "main")
        with self.assertRaisesRegex(verify_plan_ref.PlanRefError, "not an ancestor"):
            self.verify(commit=side_commit)

    def test_returns_the_same_head_used_for_ancestor_check_when_head_moves(self):
        original_git = getattr(verify_plan_ref, "_git")
        moved = False

        def moving_git(repo, *args, text=True):
            nonlocal moved
            result = original_git(repo, *args, text=text)
            if args[:2] == ("merge-base", "--is-ancestor") and not moved:
                moved = True
                race = self.repo / "race.txt"
                race.write_text("moved\n")
                self.git("add", "race.txt")
                self.git("commit", "-m", "move head during verification")
            return result

        setattr(verify_plan_ref, "_git", moving_git)
        try:
            result = self.verify()
        finally:
            setattr(verify_plan_ref, "_git", original_git)

        self.assertTrue(moved)
        self.assertNotEqual(self.commit, self.output("rev-parse", "HEAD"))
        self.assertEqual(self.commit, result["head"])


if __name__ == "__main__":
    unittest.main()

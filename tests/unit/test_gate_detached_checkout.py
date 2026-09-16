"""The PR branch name is gate identity, not a guaranteed local git ref."""

import subprocess

from scripts.check_gate import main


def test_detached_pr_uses_commit_for_diff_but_branch_for_approval(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=tmp_path, check=True, text=True, capture_output=True
        ).stdout.strip()

    git("init")
    (tmp_path / "README.md").write_text("synthetic gate fixture\n")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "feat/images")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("pass\n")
    gates = tmp_path / "docs/gates"
    gates.mkdir(parents=True)
    record = gates / "images.md"
    record.write_text(
        "---\nbranch: feat/images\nprd_approved: 2026-09-10\n"
        "design_approved: 2026-09-10\n---\nSynthetic test approval only.\n"
    )
    git("add", ".")
    git("commit", "-m", "candidate")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", head)
    git("branch", "-D", "feat/images")
    args = ["--root", str(tmp_path), "--base", base, "--head", "feat/images"]
    assert main(args) == 2  # the previous CI invocation cannot resolve the name
    assert main([*args, "--head-ref", head]) == 0
    record.write_text("---\nbranch: feat/unrelated\n---\n")
    assert main([*args, "--head-ref", head]) == 1  # never bypass branch identity

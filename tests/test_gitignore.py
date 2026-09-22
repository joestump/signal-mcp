"""The committed .gitignore must hide local agent worktrees.

A worktree left inside the checkout shows up as untracked and can be swept
into a commit. Only the committed .gitignore protects every clone, so the
matching rule has to come from it, not from .git/info/exclude or a global
excludes file on one developer's machine.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "path",
    [
        ".claude/worktrees/feature/9-ci/README.md",
        ".claude/worktrees/pr-review-c16f0d/signal_mcp/main.py",
        ".pr-review-89/README.md",
        ".pr-review-123/tests/test_config.py",
    ],
)
def test_worktree_dirs_ignored_by_committed_gitignore(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--verbose", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{path} is not ignored: {result.stderr}"
    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", f"{path} is ignored by {source}, not .gitignore"

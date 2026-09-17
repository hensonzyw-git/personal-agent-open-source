"""Print a patch covering tracked and untracked worktree content.

This is the sandbox's own diff command, pinned in
``.personal-agent/toolchain.json`` as ``registry.diff``. It mirrors, at
read-only review level, what the DAL worker's checkpoint builder does
(``_worktree_patch``): ``git diff --binary --no-ext-diff HEAD`` for tracked
changes plus one ``git diff --no-index /dev/null <path>`` per untracked file,
because a plain ``git diff HEAD`` cannot see new (untracked) files and the
worker never commits.

Run with no arguments from the repository root (the toolchain's cwd).
Exit codes: 0 with a non-empty patch on stdout, 2 when the worktree is clean
(the verification contract rejects an empty diff upstream, so a clean tree is
signalled explicitly rather than printed as empty output).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60
    )


def main() -> int:
    root = Path.cwd()

    tracked = _git("diff", "--binary", "--no-ext-diff", "HEAD")
    if tracked.returncode != 0:
        print(tracked.stderr, file=sys.stderr)
        return 1

    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode != 0:
        print(untracked.stderr, file=sys.stderr)
        return 1

    pieces = [tracked.stdout]
    for relative in filter(None, untracked.stdout.split("\0")):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            print(f"refusing unsafe untracked path: {relative}", file=sys.stderr)
            return 1
        added = _git("diff", "--binary", "--no-index", "--", "/dev/null", relative)
        # git diff --no-index exits 1 when the files differ, which is the
        # expected outcome for a new file.
        if added.returncode not in (0, 1):
            print(added.stderr, file=sys.stderr)
            return 1
        pieces.append(added.stdout)

    patch = "".join(pieces)
    if not patch.strip():
        print("worktree clean: nothing to diff", file=sys.stderr)
        return 2
    sys.stdout.write(patch)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Before committing a release: does the git index hold the tested tree?

Installs (marketplace or clone) get only what is committed. An untracked
hooks/pixi.sh or skills/ ships a plugin whose jobs server can't start and
that has no /check:* commands; an unstaged deletion ships the old file too
(commands/*.md next to skills/, an installer). Stage everything, then check:

    git add -A                    # from the repo root
    pixi run release-check        # 0 ready, 1 not ready, 2 can't tell

`release_check.py [PATH]` checks the repo holding PATH (default: this one).
Read-only: `git rev-parse` and `git ls-files` only, so it never writes the
index (nor refreshes it: GIT_OPTIONAL_LOCKS=0).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# What pixi, Python and Finder leave in a plugin folder, and the type
# declarations Claude Code lays beside a mod it loads from a folder
# (.claude-plugin/types/); ignored on purpose.
JUNK_DIRS = {".pixi", "__pycache__"}
JUNK_FILES = {".DS_Store"}
JUNK_PATHS = (".claude-plugin/types/",)
SHOW = 8


def git(repo: Path, *args: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=60, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} failed")
    return [p for p in r.stdout.split("\0") if p]


def junk(path: str) -> bool:
    parts = path.split("/")
    # Claude Code also writes a tsconfig.json extending those types at the top
    # of a plugin folder (plugins/<name>/tsconfig.json), once.
    engine_tsconfig = len(parts) == 3 and parts[0] == "plugins" and parts[2] == "tsconfig.json"
    return (bool(JUNK_DIRS & set(parts)) or parts[-1] in JUNK_FILES or path.endswith(".pyc")
            or any(f"/{p}" in f"/{path}" for p in JUNK_PATHS) or engine_tsconfig)


def problems(top: Path) -> list[tuple[str, list[str]]]:
    """(what, paths) for each way a commit of the index would differ from
    the working tree the tests ran on. Paths are relative to `top`."""
    deleted = git(top, "ls-files", "-z", "--deleted")
    gone = set(deleted)
    modified = [p for p in git(top, "ls-files", "-z", "--modified") if p not in gone]
    untracked = git(top, "ls-files", "-z", "--others", "--exclude-standard")
    # File by file, not --directory: that also lists a folder merely holding
    # ignored files (skills/ with a .DS_Store) as if the folder were ignored.
    ignored = [p for p in git(top, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                              "--", "plugins") if not junk(p)]
    return [(what, paths) for what, paths in (
        ("untracked, so installs lack them", untracked),
        ("deleted, not staged, so the commit keeps them", deleted),
        ("modified, not staged", modified),
        ("ignored in a plugin folder, so never committed; fix .gitignore or `git add -f`", ignored),
    ) if paths]


def main(argv: list[str]) -> int:
    start = Path(argv[0]) if argv else Path(__file__).resolve().parent
    try:
        top = Path(git(start, "rev-parse", "--show-toplevel")[0].strip())
        found = problems(top)
    except (OSError, subprocess.SubprocessError, RuntimeError, IndexError) as e:
        print(f"can't tell: {start} is not in a git work tree, or git failed ({e})", file=sys.stderr)
        return 2
    if not found:
        print(f"ready: the index matches the working tree ({top}); nothing untracked or unstaged")
        return 0
    print(f"not ready to commit: the index differs from the tested working tree ({top})")
    for what, paths in found:
        more = f", and {len(paths) - SHOW} more" if len(paths) > SHOW else ""
        print(f"  {what} ({len(paths)}): {', '.join(paths[:SHOW])}{more}")
    print("fix: from the repo root run `git add -A`, check `git status`, then run this again")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

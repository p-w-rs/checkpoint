"""Where checkpoint's TUI acceptance scripts get the plugin from (brief 14.1:
acceptance on installed copies).

    plugin, plugin_dirs, data, info = provide(root, cfg, installed)

- installed=False: a copy of what `git add -A` would commit, loaded with
  `--plugin-dir` (Claude Code writes types into a mod folder it loads, so
  never the checkout); its runtime goes to `<cfg>/plugins/data/check-inline`.
- installed=True: the normal installation, `check@checkpoint` from a local
  git-source marketplace (tests/tui install.Hermetic: a snapshot of the
  working tree served by the smart-HTTP git server, `claude plugin
  marketplace add` + `claude plugin install` into the scratch config `cfg`);
  sessions then load it from the config (no `--plugin-dir`), from its cache
  path `<cfg>/plugins/cache/checkpoint/check/<version>`, with its data in
  `<cfg>/plugins/data/check-checkpoint`.

`plugin` is the folder the sessions run (for `env.sh` calls and imports),
`plugin_dirs` what to pass to Session, `data` CLAUDE_PLUGIN_DATA, `info` the
evidence (id, version, cache path, the `claude plugin` calls and their exit
codes).

    env = cli_env(cfg, data, home, envd)

The environment for checkpoint's CLI run from the test process (`env.sh run
...` outside a session): the sessions' state, their plugin data folder and the
gateway's contract and env file as the sessions get them, whatever the
caller's own environment says.

    note_marked(marker); kill_marked(marker); left = kill_started()

What the scripted commands start, ended by the scripts that start them
(0.7.2: on Linux, acceptance_journal and acceptance_recovery left `sleep 300`
behind: killing the shell whose command line names the marker leaves its
`sleep` child running, reparented, and that child's command line names
nothing). A marked process's tree is its descendants plus the rest of its
process group when that group is the marked shell's own or its leader is
gone (Claude Code killed); `note_marked` remembers a tree while it runs (the
shell may die before the cleanup), `kill_marked` SIGKILLs one now, and
`kill_started` everything remembered that is still the same process (start
time and command), returning what survived. Never a process outside those
trees (this user's only, never the harness or its process group).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from tui import install, safety

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
REPO = PLUGIN.parents[1]


def provide(root: Path, cfg: Path, installed: bool,
            extra_env: dict | None = None) -> tuple[Path, list[Path], Path, dict]:
    if not installed:
        plugin = root / "plugin"
        install.snapshot_tree(PLUGIN, plugin)
        cfg.mkdir(parents=True, exist_ok=True)
        return plugin, [plugin], cfg / "plugins" / "data" / "check-inline", {"mode": "copy"}
    with install.Hermetic(root / "inst", config_dir=cfg, extra_env=extra_env) as h:
        p = h.install_from(REPO, repo="checkpoint", plugin="check")
        hist = [{"argv": r.argv[1:], "rc": r.rc} for r in h.inst.history]
    if p.cache_path is None or not (Path(p.cache_path) / "hooks" / "env.sh").is_file():
        raise RuntimeError(f"check@checkpoint did not install into {cfg} ({hist})")
    return Path(p.cache_path), [], p.data_path, {"mode": "installed", "id": p.id, "version": p.version,
                                                 "cache": str(p.cache_path), "data": str(p.data_path),
                                                 "history": hist}


def cli_env(cfg: Path, data: Path, home: Path, envd: str = "", base: dict | None = None) -> dict:
    """`base` (default os.environ) with the sessions' values set: CHECKPOINT_HOME `home`,
    CLAUDE_PLUGIN_DATA `data` (lib.sh takes it before CHECKPOINT_ENVD, so a caller's own value, a
    scratch folder or another plugin's, sent `env.sh run status` to an env that isn't there: exit 75,
    nothing printed), CHECKPOINT_ENVD `envd` (what SessionStart recorded in rt/<session>), and the
    gateway's contract and env file from the config's state root (unset, checkpoint looks for the real
    ~/.config/gateway/run/contract.json and calls the daemon it names)."""
    st = safety.state_env(safety.state_root_for(cfg))
    e = dict(os.environ if base is None else base)
    e.update(CHECKPOINT_HOME=str(home), CLAUDE_PLUGIN_DATA=str(data), CHECKPOINT_ENVD=envd,
             CHECKPOINT_GATEWAY_CONTRACT=st["CHECKPOINT_GATEWAY_CONTRACT"],
             CHECKPOINT_GATEWAY_ENV=st["CHECKPOINT_GATEWAY_ENV"])
    return e


def merge_settings(cfg: Path, extra: dict) -> None:
    """Write `extra` into the config's settings.json without dropping what an
    installation put there (enabledPlugins)."""
    p = cfg / "settings.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(extra)
    p.write_text(json.dumps(cur, indent=1))
    os.chmod(p, 0o600)


# -- what the scripted commands start --------------------------------------------------

STARTED: dict[int, str] = {}   # pid -> identity (start time and command) of every tree noted or killed


def _ps() -> dict[int, tuple[int, int, str]]:
    """pid -> (ppid, pgid, command line) of this user's live processes (one ps)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,uid=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    me, table = os.getuid(), {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and all(p.isdigit() for p in parts[:4]) and int(parts[3]) == me:
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[4] if len(parts) == 5 else "")
    return table


def marked_tree(marker: str) -> dict[int, str]:
    """pid -> identity of every live process whose command line names `marker`, everything they started,
    and the rest of their process groups when a group is a marked process's own or its leader is gone;
    never this process or a member of its process group."""
    t = _ps()
    me, mine = os.getpid(), os.getpgrp()
    kids: dict[int, list[int]] = {}
    for p, (pp, _g, _c) in t.items():
        kids.setdefault(pp, []).append(p)

    def tree(roots: list[int]) -> set[int]:
        found: set[int] = set()
        todo = list(roots)
        while todo:
            p = todo.pop()
            if p in found or p == me or p not in t or t[p][1] == mine:
                continue
            found.add(p)
            todo.extend(kids.get(p, []))
        return found

    roots = [p for p, (_pp, _g, cmd) in t.items() if marker in cmd]
    found = tree(roots)
    leaders = {g for g in (t[p][1] for p in found) if g > 1 and g != mine and (g in roots or g not in t)}
    found |= tree([p for p, (_pp, g, _c) in t.items() if g in leaders])
    return safety.identities(sorted(found))


def note_marked(marker: str) -> dict[int, str]:
    """Remember the marked processes' trees while they run (kill_started ends them later)."""
    ids = marked_tree(marker)
    STARTED.update(ids)
    return ids


def _kill(ids: dict[int, str], wait: float = 5.0) -> list[int]:
    """SIGKILL each pid that is still the process it was listed as; the ones still there after `wait` s."""
    def same() -> list[int]:
        now = safety.identities(list(ids))
        return [p for p, ident in ids.items() if now.get(p) == ident]
    for p in same():
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    end = time.time() + wait
    while time.time() < end and same():
        time.sleep(0.1)
    return same()


def kill_marked(marker: str) -> list[int]:
    """End the marked processes' trees now (remembered too); what survived."""
    return _kill(note_marked(marker))


def kill_started() -> list[str]:
    """End every remembered tree's processes that still run; 'pid command' of what survived."""
    left = _kill(dict(STARTED))
    t = _ps()
    return [f"{p} {t.get(p, (0, 0, '?'))[2][:120]}" for p in left]

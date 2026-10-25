"""Safely back up experiment artifacts during a temporary GPU allocation.

Uses ``rclone copy`` (not ``sync``), so a transient or partially written local
directory cannot delete an existing Drive backup. Configure rclone once, then:

    python -m experiments.backup copy results vectors checkpoints \
      --remote gdrive:algoverse/nla-a100-2026-08

    python -m experiments.backup watch results vectors checkpoints \
      --remote gdrive:algoverse/nla-a100-2026-08 --interval-minutes 15
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path


def _rclone_copy(sources: list[str], remote: str) -> None:
    executable = shutil.which("rclone")
    if executable is None:
        raise SystemExit("rclone is not installed; install it and run `rclone config` first")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for source in sources:
        path = Path(source)
        if not path.exists():
            print(f"[{stamp}] skip missing: {path}")
            continue
        destination = remote.rstrip("/") + "/" + path.name
        print(f"[{stamp}] copy {path} -> {destination}")
        subprocess.run([executable, "copy", str(path), destination,
                        "--create-empty-src-dirs", "--checksum"], check=True)


def _git_push(message: str) -> None:
    """Explicit code-only backup; artifacts should go to Drive instead."""
    subprocess.run(["git", "add", "experiments", ".gitignore"], check=True)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("no staged code changes to commit")
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("copy", "watch"):
        command = commands.add_parser(name)
        command.add_argument("sources", nargs="+", help="result/vector/checkpoint directories")
        command.add_argument("--remote", required=True, help="rclone remote, e.g. gdrive:folder")
        command.add_argument("--interval-minutes", type=float, default=15)
    git_command = commands.add_parser("git-push")
    git_command.add_argument("--message", default="Add NLA experiment implementation")
    args = parser.parse_args(argv)
    if args.command == "git-push":
        _git_push(args.message)
        return
    if args.command == "copy":
        _rclone_copy(args.sources, args.remote)
        return
    while True:
        _rclone_copy(args.sources, args.remote)
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()

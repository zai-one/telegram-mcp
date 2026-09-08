"""Read-only Git/source baseline; writes only the explicitly selected receipt."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict:
    records = git(root, "worktree", "list", "--porcelain").decode().split("\n\n")
    trees = []
    for record in records:
        if not record.strip():
            continue
        facts = dict(line.split(" ", 1) if " " in line else (line, True)
                     for line in record.splitlines())
        path = Path(facts["worktree"])
        item = {"registration": facts, "available": path.is_dir()}
        if not path.is_dir():
            trees.append(item)
            continue
        item["head"] = git(path, "rev-parse", "HEAD").decode().strip()
        item["status"] = git(path, "status", "--porcelain=v1", "-z",
                             "--untracked-files=all").decode("utf-8", "replace").split("\0")
        # Tracked content fingerprints preserve dirty states without storing content.
        tracked = git(path, "ls-files", "-z").decode("utf-8").split("\0")
        item["tracked_sha256"] = {
            name: digest(path / name) for name in tracked if name and (path / name).is_file()
        }
        trees.append(item)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "command_plane": "local Windows Git; no remote fetch or live requests",
        "root": str(root),
        "refs": git(root, "show-ref").decode().splitlines(),
        "remotes": git(root, "remote", "-v").decode().splitlines(),
        "worktrees": trees,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("receipt exists; choose a new path")
    document = snapshot(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"receipt": str(args.output), "worktrees": len(document["worktrees"]),
                      "available": sum(t["available"] for t in document["worktrees"])}))


if __name__ == "__main__":
    main()

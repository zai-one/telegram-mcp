"""Compare existing worktree states without treating new owned worktrees as drift."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("receipt already exists")
    before, after = (json.loads(path.read_text(encoding="utf-8")) for path in (args.before, args.after))
    current = {item["registration"]["worktree"]: item for item in after["worktrees"]}
    entries = []
    for original in before["worktrees"]:
        path = original["registration"]["worktree"]
        newer = current.pop(path, None)
        fields = ["registration", "available", "head", "status", "tracked_sha256"]
        changed = fields if newer is None else [name for name in fields if original.get(name) != newer.get(name)]
        entries.append({"worktree": path, "unchanged": not changed, "changed_fields": changed})
    old_refs = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in before["refs"]}
    new_refs = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in after["refs"]}
    changed_refs = [ref for ref, sha in old_refs.items() if new_refs.get(ref) != sha]
    passed = all(entry["unchanged"] for entry in entries) and not changed_refs and before["remotes"] == after["remotes"]
    receipt = {"passed": passed, "worktrees": entries, "added_worktrees": list(current),
               "changed_existing_refs": changed_refs, "remotes_unchanged": before["remotes"] == after["remotes"]}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "original_worktrees": len(entries), "unchanged": sum(x["unchanged"] for x in entries), "added": len(current)}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

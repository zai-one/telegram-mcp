"""Normalize only this newly created repository before reproducible builds."""
import json
import subprocess
from pathlib import Path

root = Path(r"D:\ZAI\MCP\Telegram")
provenance = root / "SOURCE_PROVENANCE.json"
data = json.loads(provenance.read_text(encoding="utf-8"))
data["runtime_status"] = "local independent runtime candidate; no publication or live deployment"
data["standalone_changes"] = [
    "server-owned bindings and RS256/scopes; explicit stdio authorization",
    "private SQLite outbox/quota/audit; pending unknown sends never resubmitted",
    "known local pre-dispatch denials fail without weakening ambiguous-send protection",
    "one credential fingerprint per embedded process; permitted env keys and private upstream namespace",
    "original 12 MCP schemas/descriptions retained; central store policy injected separately",
]
provenance.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
paths = subprocess.check_output(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode().split("\0")
for name in set(filter(None, paths)):
    path = root / name
    assert path.resolve().is_relative_to(root.resolve()) and not path.is_symlink()
    raw = path.read_bytes()
    raw.decode("utf-8")
    if b"\r\n" in raw:
        path.write_bytes(raw.replace(b"\r\n", b"\n"))
print("Normalized owned candidate text files to LF")

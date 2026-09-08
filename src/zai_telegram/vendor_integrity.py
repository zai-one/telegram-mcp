"""Verify our private upstream namespace instead of importing a PyPI name collision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def assert_pinned_vendor() -> None:
    root = Path(__file__).parent / "_vendor"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, record in manifest["files"].items():
        candidate = root / name
        path = candidate.resolve()
        if (
            not path.is_relative_to(root.resolve())
            or candidate.is_symlink()
            or any(parent.is_symlink() for parent in candidate.parents if parent != root.parent)
            or not path.is_file()
        ):
            raise RuntimeError("pinned Telegram upstream file unavailable")
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if digest != record["packaged_sha256"]:
            raise RuntimeError("pinned Telegram upstream integrity mismatch")

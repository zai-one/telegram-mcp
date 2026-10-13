"""Token budget for the agent-facing surface (run in CI via tests/test_agent_layer.py).

Checks: instructions <= 1300 chars, each tool description <= 300 chars, total
tools/list JSON for the default toolsets <= BUDGET_BYTES, skills frontmatter valid.
Usage: python scripts/token_budget.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# +wait_for_new_message (events) costs ~600 B; budget grows only with a default tool.
BUDGET_BYTES = {"default": 11_700, "all": 12_600}
# Always-loaded agent context = instructions + tools/list + every skill frontmatter.
# Estimated tokens = bytes / 4 (conservative for JSON/English; Russian costs more,
# but frontmatters are mostly English). Budget per preset.
CONTEXT_TOKEN_BUDGET = {"default": 4_000, "all": 4_200}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter_bytes(root: Path = ROOT) -> int:
    total = 0
    for skill in sorted((root / "skills").iterdir()):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        total += len(match.group(1).encode()) if match else 0
    return total


def skill_problems(root: Path = ROOT) -> list[str]:
    problems = []
    for skill in sorted((root / "skills").iterdir()):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not match:
            problems.append(f"{skill.name}: missing frontmatter")
            continue
        meta = dict(
            line.split(":", 1)
            for line in match.group(1).splitlines()
            if ":" in line and not line.startswith(" ")
        )
        name, description = meta.get("name", "").strip(), meta.get("description", "").strip()
        if name != skill.name or not NAME.match(name) or len(name) > 64:
            problems.append(f"{skill.name}: name must equal folder and match a-z0-9-")
        if not 1 <= len(description) <= 300:
            problems.append(f"{skill.name}: description must be 1..300 chars (got {len(description)})")
        if len(match.group(2).splitlines()) > 80:
            problems.append(f"{skill.name}: SKILL.md body over 80 lines")
        for ref in re.findall(r"`((?:references|templates|scripts|assets)/[^`]+)`", match.group(2)):
            if not (skill / ref).is_file():
                problems.append(f"{skill.name}: missing {ref}")
    return problems


async def surface_sizes(config) -> dict[str, dict]:
    from fastmcp import Client

    from zai_telegram.server import create_server
    from zai_telegram.toolsets import build_surface

    out = {}
    for preset in BUDGET_BYTES:
        server = create_server(config, transport="stdio", adapter=object(), surface=build_surface(preset))
        async with Client(server) as client:
            tools = await client.list_tools()
        payload = json.dumps([tool.model_dump(mode="json", exclude_none=True) for tool in tools])
        out[preset] = {
            "tools": len(tools),
            "bytes": len(payload.encode()),
            "instructions": len(server.instructions or ""),
            "instructions_bytes": len((server.instructions or "").encode()),
            "long_descriptions": [t.name for t in tools if len(t.description or "") > 300],
        }
    return out


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from zai_telegram.config import SCOPES, ServiceConfig

    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "fixture.env"
        secret.write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=x\nTELEGRAM_SESSION_STRING=y\n")
        secret.chmod(0o600)
        config = ServiceConfig(
            state_path=Path(tmp) / "s.sqlite",
            secret_path=secret,
            local_scopes=SCOPES,
            telegram_write_enabled=True,
        )
        sizes = asyncio.run(surface_sizes(config))
    problems = skill_problems()
    skills_bytes = frontmatter_bytes()
    for preset, info in sizes.items():
        info["skill_frontmatter_bytes"] = skills_bytes
        info["context_tokens_est"] = (info["bytes"] + info["instructions_bytes"] + skills_bytes) // 4
        if info["context_tokens_est"] > CONTEXT_TOKEN_BUDGET[preset]:
            limit = CONTEXT_TOKEN_BUDGET[preset]
            problems.append(f"{preset}: agent context ~{info['context_tokens_est']} tokens > {limit}")
        if info["bytes"] > BUDGET_BYTES[preset]:
            problems.append(f"{preset}: tools/list {info['bytes']} B > {BUDGET_BYTES[preset]} B")
        if info["instructions"] > 1300:
            problems.append(f"{preset}: instructions {info['instructions']} > 1300 chars")
        problems += [f"{preset}: description over 300 chars: {name}" for name in info["long_descriptions"]]
    print(json.dumps({"sizes": sizes, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

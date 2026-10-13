"""Router eval: messy requests (EN/RU) must reach the right skill/guide and first tool.

Path checked per case: router line (instructions) -> named skill/guide exists and is
reachable -> that skill/guide (or the router line) names the first tool -> the tool is
registered on some surface. Plus reachability: every tool, skill and guide is reachable
from the router (directly, or via a skill/guide/playbook it names).
"""

import json
import re
from pathlib import Path

import pytest
from fastmcp import Client
from test_runtime import FixtureAdapter

from zai_telegram.agent_layer import GUIDE_TOPICS, SKILLS, build_instructions, guide
from zai_telegram.server import create_server
from zai_telegram.toolsets import KNOWN_TOOLS, build_surface

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests" / "eval" / "router_cases.json").read_text(encoding="utf-8"))
ROUTER = build_instructions(build_surface())


def router_line(route: str) -> str:
    lines = [line for line in ROUTER.splitlines() if line.startswith("- ")]
    words = [w for w in re.split(r"[/ ]", route) if w]
    hits = [line for line in lines if all(w.casefold() in line.casefold() for w in words)]
    assert hits, f"no router line for route {route!r}"
    return hits[0]


def skill_text(name: str) -> str:
    folder = ROOT / "skills" / name
    return "\n".join(p.read_text(encoding="utf-8") for p in folder.rglob("*.md"))


def test_eval_has_twenty_plus_mixed_language_cases():
    assert len(CASES) >= 20
    assert sum(bool(re.search("[а-яё]", c["request"], re.I)) for c in CASES) >= 8


@pytest.mark.parametrize("case", CASES, ids=[c["request"][:40] for c in CASES])
def test_case_path_router_skill_guide_tool(case):
    if case.get("resource"):
        assert case["resource"] in ROUTER.splitlines()[-1]  # fallback line
        return
    line = router_line(case["route"])
    detail = ""
    if case.get("skill"):
        assert f"skill {case['skill']}" in line
        detail += skill_text(case["skill"])
    if case.get("guide"):
        assert re.search(rf"guide [^>]*\b{re.escape(case['guide'])}\b", line)
        detail += guide(case["guide"])
    tool = case["first_tool"]
    if tool:
        assert tool in KNOWN_TOOLS
        assert tool in line or tool in detail, f"{tool} not reachable via {line!r}"


def test_skill_descriptions_trigger_on_colloquial_requests():
    """Each skill's description shares a trigger word with the requests routed to it."""
    for name in SKILLS:
        text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        description = re.search(r"^description: (.*)$", text, re.M).group(1).casefold()
        requests = [c["request"].casefold() for c in CASES if c.get("skill") == name]
        if not requests:
            continue
        stems = {w[:5] for w in re.findall(r"[\wа-яё]{5,}", description)}
        assert any(any(stem in request for stem in stems) for request in requests), name


async def test_every_capability_is_reachable_from_router(config):
    lines = ROUTER
    reachable_docs = lines
    named_skills = set(re.findall(r"skill (telegram-[a-z-]+)", lines))
    named_guides = {g for g in GUIDE_TOPICS if re.search(rf"guide [^\n]*\b{re.escape(g)}\b", lines)}
    for skill in named_skills:
        reachable_docs += skill_text(skill)
    for topic in named_guides:
        reachable_docs += guide(topic)
    assert "telegram://playbook" in lines.splitlines()[-1]
    # Skills and guides: named by the router, or listed by the playbook it points to.
    surface = build_surface("all,advanced_read")
    async with Client(
        create_server(config, transport="stdio", adapter=FixtureAdapter(), surface=surface)
    ) as c:
        playbook = (await c.read_resource("telegram://playbook"))[0].text
        exposed = {tool.name for tool in await c.list_tools()}
    assert set(SKILLS) == {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()}
    for skill in SKILLS:
        assert skill in named_skills or skill in playbook, skill
    for topic in GUIDE_TOPICS:
        assert f"telegram://guide/{topic}" in playbook, topic
    # Tools: every registered tool is named by the router or a skill/guide it names.
    unreachable = {
        tool
        for tool in KNOWN_TOOLS | exposed
        if tool not in reachable_docs and tool != "telegram_check_update"
    }
    assert not unreachable, f"unreachable from router: {sorted(unreachable)}"

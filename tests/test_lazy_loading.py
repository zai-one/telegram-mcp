"""TELEGRAM_TOOL_LOADING=lazy: small start surface, runtime toolset enabling, list_changed."""

from __future__ import annotations

import json

import mcp.types
import pytest
from fastmcp import Client, FastMCP
from test_runtime import FixtureAdapter

from zai_telegram import lazy_loading
from zai_telegram.lazy_loading import estimate_tokens
from zai_telegram.server import create_server
from zai_telegram.toolsets import LAZY_PROFILE as PROFILE

# Session-scoped enablement needs a session: handshake-era (initialize) clients have one. Modern
# sessionless clients (2026-07-28 server/discover) get a new session per request; see README.
ENABLE_SET = "chat_insights"
OPTIN_SET = "advanced_read"
_CONFIG = {}


@pytest.fixture(autouse=True)
def _config(config):
    _CONFIG["value"] = config


def build(tmp_path):
    return create_server(_CONFIG["value"], transport="stdio", adapter=FixtureAdapter())


ENABLE = f"{PROFILE.service}_enable_toolsets"
ENV = PROFILE.env
LAZY_BUDGET_TOKENS = 3_000


async def startup_tokens(client: Client) -> int:
    tools = await client.list_tools()
    listed = [tool.model_dump(mode="json", exclude_none=True, by_alias=True) for tool in tools]
    return estimate_tokens(listed) + estimate_tokens(client.initialize_result.instructions or "")


class Notices:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, message) -> None:
        root = getattr(message, "root", message)
        if isinstance(root, mcp.types.ToolListChangedNotification):
            self.seen.append("tools/list_changed")


def core(eager: set[str]) -> set[str]:
    return ((set(PROFILE.toolsets.get("meta", ())) | set(PROFILE.lazy_core)) & eager) | {ENABLE}


async def eager_names(tmp_path, monkeypatch) -> set[str]:
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "eager")
    async with Client(build(tmp_path / "eager"), mode="legacy") as client:
        return {tool.name for tool in await client.list_tools()}


async def test_eager_is_default_and_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv(f"{ENV}_TOOL_LOADING", raising=False)
    async with Client(build(tmp_path), mode="legacy") as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "Lazy tools" not in (client.initialize_result.instructions or "")
    assert ENABLE not in names
    assert names == await eager_names(tmp_path, monkeypatch)


async def test_lazy_start_surface(tmp_path, monkeypatch):
    eager = await eager_names(tmp_path, monkeypatch)
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    async with Client(build(tmp_path / "lazy"), mode="legacy") as client:
        names = {tool.name for tool in await client.list_tools()}
        assert f"{ENABLE}()" in client.initialize_result.instructions
        listing = (await client.call_tool(ENABLE, {})).data
    assert names == core(eager)
    assert set(PROFILE.lazy_core) & eager <= names
    assert ENABLE_SET in {row["toolset"] for row in listing["toolsets"]}
    assert all(row["description"] and row["token_cost"] > 0 for row in listing["toolsets"])
    offered = {name for row in listing["toolsets"] for name in row["tools"]}
    assert offered | names == eager | {ENABLE}


async def test_handshake_clients_see_tools_list_changed(tmp_path, monkeypatch):
    # Handshake-era clients get tools.listChanged=true. Modern-era (server/discover) clients only
    # get list_changed over subscriptions/listen, which this server does not serve: they re-list.
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    async with Client(build(tmp_path), mode="legacy") as client:
        assert client.initialize_result.capabilities.tools.list_changed is True


async def test_enable_registers_tools_and_notifies(tmp_path, monkeypatch):
    eager = await eager_names(tmp_path, monkeypatch)
    expected = set(PROFILE.toolsets[ENABLE_SET]) - core(eager)
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    notices = Notices()
    async with Client(build(tmp_path / "lazy"), mode="legacy", message_handler=notices) as client:
        result = (await client.call_tool(ENABLE, {"toolsets": [ENABLE_SET]})).data
        names = {tool.name for tool in await client.list_tools()}
    assert expected and set(result["added_tools"]) == expected
    assert names == core(eager) | expected
    assert notices.seen == ["tools/list_changed"]
    assert "reconnect" in result["hint"] and f"{ENV}_TOOL_LOADING=eager" in result["hint"]


async def test_enable_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    notices = Notices()
    async with Client(build(tmp_path), mode="legacy", message_handler=notices) as client:
        await client.call_tool(ENABLE, {"toolsets": [ENABLE_SET]})
        again = (await client.call_tool(ENABLE, {"toolsets": [ENABLE_SET]})).data
        names = [tool.name for tool in await client.list_tools()]
        everything = (await client.call_tool(ENABLE, {"toolsets": ["all"]})).data
        twice = (await client.call_tool(ENABLE, {"toolsets": ["all"]})).data
    assert again["added_tools"] == [] and again["already_enabled"] is True
    assert len(names) == len(set(names))
    assert twice["added_tools"] == [] and not set(everything["added_tools"]) & set(names)
    assert notices.seen == ["tools/list_changed", "tools/list_changed"]


async def test_lazy_respects_read_only_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    monkeypatch.setenv(f"{ENV}_TOOLSETS", "all")
    monkeypatch.setenv(f"{ENV}_READ_ONLY", "1")
    async with Client(build(tmp_path / "ro"), mode="legacy") as client:
        await client.call_tool(ENABLE, {"toolsets": ["all"]})
        tools = {tool.name for tool in await client.list_tools()}
    # Same surface as eager read-only (local-only telegram_acknowledge_poll stays), no write tools.
    assert tools - {ENABLE} == await eager_names(tmp_path / "ro-eager", monkeypatch)
    assert not tools & {"telegram_send_message", "telegram_send_many", "telegram_write"}
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    monkeypatch.delenv(f"{ENV}_READ_ONLY")
    monkeypatch.setenv(f"{ENV}_TOOLSETS", "default")
    async with Client(build(tmp_path / "allow"), mode="legacy") as client:
        refused = (await client.call_tool(ENABLE, {"toolsets": [OPTIN_SET]})).data
    assert refused["error"] == "unknown_toolset" and OPTIN_SET not in refused["available"]


def test_invalid_mode_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "sometimes")
    with pytest.raises(ValueError):
        build(tmp_path)


async def test_lazy_startup_budget(tmp_path, monkeypatch, capsys):
    costs = {}
    for mode in ("eager", "lazy"):
        monkeypatch.setenv(f"{ENV}_TOOL_LOADING", mode)
        async with Client(build(tmp_path / mode), mode="legacy") as client:
            costs[mode] = await startup_tokens(client)
    assert costs["lazy"] <= LAZY_BUDGET_TOKENS
    assert costs["lazy"] < costs["eager"]
    with capsys.disabled():
        print(f"\nstartup tokens {json.dumps(costs)}")


@pytest.mark.parametrize(("answer", "mode"), [("lazy", "lazy"), ("", "eager"), ("whatever", "eager")])
def test_setup_wizard_asks_eager_or_lazy(answer, mode, monkeypatch):
    from zai_telegram import setup

    asked = []

    def fake_input(prompt):
        asked.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    assert setup._tool_loading() == mode
    assert "lazy" in asked[0] and "eager" in asked[0]


async def test_reregistration_keeps_unenabled_tools_hidden(monkeypatch):
    """Hot account activation re-registers every tool; not-enabled ones stay hidden."""
    monkeypatch.setenv("TELEGRAM_TOOL_LOADING", "lazy")
    server = FastMCP("t")

    def telegram_list_chats() -> str:
        return "chats"

    def telegram_get_admins() -> str:
        return "v1"

    for function in (telegram_list_chats, telegram_get_admins):
        server.tool(function)
    state = lazy_loading.install(server, PROFILE, {"telegram_list_chats", "telegram_get_admins"})
    assert "telegram_get_admins" not in state.listed

    def telegram_get_admins() -> str:  # noqa: F811 - re-registration with a new signature
        return "v2"

    server.tool(telegram_get_admins)
    state.resync()
    async with Client(server, mode="legacy") as client:
        assert "telegram_get_admins" not in {tool.name for tool in await client.list_tools()}
        await client.call_tool("telegram_enable_toolsets", {"toolsets": ["chat_insights"]})
        assert (await client.call_tool("telegram_get_admins", {})).data == "v2"


async def test_enablement_is_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv(f"{ENV}_TOOL_LOADING", "lazy")
    shared = build(tmp_path / "shared")
    async with Client(shared, mode="legacy") as first, Client(shared, mode="legacy") as second:
        before = {tool.name for tool in await second.list_tools()}
        result = (await first.call_tool(ENABLE, {"toolsets": ["all"]})).data
        assert result["scope"] == "session" and result["added_tools"]
        assert set(result["added_tools"]) <= {tool.name for tool in await first.list_tools()}
        assert {tool.name for tool in await second.list_tools()} == before
        with pytest.raises(Exception):  # noqa: B017 - hidden for this session: unknown tool
            await second.call_tool(result["added_tools"][0], {})

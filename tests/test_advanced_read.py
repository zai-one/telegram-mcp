"""Opt-in generic telegram_read: reviewed allowlist, ACL, routing. Mocks only."""

import ast
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram.adapter import TELEGRAM_READ_ALLOWLIST, TelegramAdapter
from zai_telegram.server import create_server
from zai_telegram.toolsets import build_surface

READ = "telegram_read"
VENDOR = Path(__file__).resolve().parents[1] / "src" / "zai_telegram" / "_vendor" / "telegram_mcp" / "tools"


class ReadAdapter(FixtureAdapter):
    async def call(self, tool, args):
        self.calls.append((tool, args))
        TelegramAdapter._upstream_call(tool, args)
        return {"ok": True}


def vendor_read_only_tools():
    names = set()
    for path in VENDOR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef):
                for deco in node.decorator_list:
                    if "readOnlyHint=True" in ast.unparse(deco):
                        names.add(node.name)
    return names


def test_allowlist_is_vendor_read_only_and_excludes_risky_reads():
    assert vendor_read_only_tools() >= TELEGRAM_READ_ALLOWLIST
    assert not TELEGRAM_READ_ALLOWLIST & {"export_chat_invite", "get_invite_link", "get_gif_search"}
    assert len(TELEGRAM_READ_ALLOWLIST) >= 40


async def names(config, surface):
    async with Client(create_server(config, transport="stdio", adapter=ReadAdapter(), surface=surface)) as c:
        return {tool.name for tool in await c.list_tools()}


async def test_opt_in_only(config):
    assert "telegram_read" not in await names(config, build_surface("all"))
    assert "telegram_read" in await names(config, build_surface("default,advanced_read"))


async def test_routes_reviewed_reads_and_refuses_others(config):
    adapter = ReadAdapter()
    surface = build_surface("default,advanced_read")
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        await client.call_tool(
            "telegram_read", {"tool": "get_pinned_messages", "arguments": {"chat_id": "101", "account": "x"}}
        )
        for bad in ({"tool": "send_message", "arguments": {"chat_id": "1"}}, {"tool": "export_chat_invite"}):
            with pytest.raises(ToolError):
                await client.call_tool("telegram_read", bad)
        with pytest.raises(ToolError):
            await client.call_tool(
                "telegram_read", {"tool": "get_participants", "arguments": {"limit": 9999}}
            )
    tool, args = adapter.calls[0]
    assert tool == "telegram_read" and args["arguments"] == {"chat_id": "101", "account": "x"}
    upstream, values = TelegramAdapter._upstream_call(tool, args)
    assert upstream == "get_pinned_messages" and values == {"chat_id": "101", "account": "default"}


async def test_allowlist_blocks_other_chats_and_account_wide_reads(config):
    adapter = ReadAdapter()
    surface = build_surface("default,advanced_read", allowed_chats=["101"])
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        await client.call_tool(
            "telegram_read", {"tool": "get_pinned_messages", "arguments": {"chat_id": "101"}}
        )
        for bad in (
            {"tool": "get_pinned_messages", "arguments": {"chat_id": "202"}},
            {"tool": "list_contacts", "arguments": {}},
            {"tool": "get_common_chats", "arguments": {"user_id": "999"}},
        ):
            with pytest.raises(ToolError):
                await client.call_tool("telegram_read", bad)
    assert len(adapter.calls) == 1

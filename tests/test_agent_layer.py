import importlib.util
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram.agent_layer import GUIDE_TOPICS, TOOL_DESCRIPTIONS, guide
from zai_telegram.server import create_server
from zai_telegram.toolsets import ALL_TOOLS, KNOWN_TOOLS, build_surface, surface_from_env

ROOT = Path(__file__).resolve().parents[1]
SEND = {"chat_id": "101", "message": "synthetic content", "idempotency_key": "stable"}


async def names(config, surface):
    async with Client(
        create_server(config, transport="stdio", adapter=FixtureAdapter(), surface=surface)
    ) as c:
        return {tool.name for tool in await c.list_tools()}


async def test_unset_env_keeps_full_backward_compatible_surface(config, monkeypatch):
    for key in ("TELEGRAM_TOOLSETS", "TELEGRAM_READ_ONLY", "TELEGRAM_ALLOWED_CHATS", "TELEGRAM_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    # telegram_list_accounts appears only for multi-account bindings.
    assert await names(config, surface_from_env()) == set(ALL_TOOLS) - {"telegram_list_accounts"}


async def test_read_only_and_default_presets(config):
    read_only = await names(config, build_surface("all", read_only=True))
    assert not read_only & {"telegram_send_message", "telegram_write", "telegram_send_many"}
    assert "telegram_get_inbox" in read_only
    default = await names(config, build_surface("default"))
    assert "telegram_write" not in default and "telegram_send_message" in default
    core = await names(config, build_surface("core", extra_tools=["telegram_get_messages"]))
    assert "telegram_get_messages" in core and "telegram_search_messages" not in core
    with pytest.raises(ValueError):
        build_surface("nope")


def test_env_parsing():
    surface = surface_from_env(
        {
            "TELEGRAM_TOOLSETS": "core,media",
            "TELEGRAM_DISABLED_TOOLS": "telegram_check_update",
            "TELEGRAM_ALLOWED_CHATS": "101, @Team",
        }
    )
    assert "telegram_write" in surface.tools and "telegram_check_update" not in surface.tools
    assert "send_file" in surface.write_tools and "ban_user" not in surface.write_tools
    assert surface.chats.permits("101") and surface.chats.permits("@team") and not surface.chats.permits("7")


async def test_write_category_gate_returns_structured_error(config):
    surface = build_surface("core,messages_write,media")
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "telegram_write", {"tool": "ban_user", "arguments": {"chat_id": "1", "user_id": "2"}}
            )
    envelope = json.loads(str(caught.value))
    assert envelope["code"] == "tool_disabled" and envelope["retryable"] is False
    assert envelope["hint"] and envelope["docs"] == "telegram://guide/errors#tool_disabled"
    assert adapter.calls == []


async def test_chat_allowlist_denies_by_default_and_filters_listings(config):
    surface = build_surface(allowed_chats=["101"])
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        await client.call_tool("telegram_send_message", SEND)
        for tool, args in (
            ("telegram_send_message", {**SEND, "chat_id": "202", "idempotency_key": "other"}),
            ("telegram_get_messages", {"chat_id": "202"}),
            (
                "telegram_write",
                {"tool": "forward_message", "arguments": {"from_chat_id": "101", "to_chat_id": "9"}},
            ),
            ("telegram_poll_messages", {"chat_ids": ["101", "9"]}),
        ):
            with pytest.raises(ToolError, match="chat_access_denied"):
                await client.call_tool(tool, args)
        listed = (await client.call_tool("telegram_list_chats", {})).structured_content
    assert [call[0] for call in adapter.calls].count("send_message") == 1
    assert "101" in json.dumps(listed)


async def test_chat_allowlist_denies_untargeted_and_account_wide_generic_writes(config):
    """Regression: telegram_write calls that name no allowed chat must fail closed."""
    surface = build_surface(allowed_chats=["101", "@team"])
    adapter = FixtureAdapter()
    denied = (
        ("join_chat_by_link", {"link": "https://t.me/+abc"}),
        ("import_chat_invite", {"hash": "abc"}),
        ("subscribe_public_channel", {"channel": "@other"}),
        ("block_user", {"user_id": "7"}),
        ("unblock_user", {"user_id": 7}),
        ("delete_contact", {"user_id": "7"}),
        ("create_group", {"title": "x", "user_ids": ["101"]}),
        ("create_channel", {"title": "x"}),
        ("update_profile", {"about": "x"}),
        ("set_privacy_settings", {"key": "phone", "allow_users": ["101"]}),
        ("create_folder", {"title": "x", "chat_ids": ["101"], "groups": True}),
        ("add_contact", {"phone": "+1", "first_name": "x", "username": "team"}),
        ("send_message", {"message": "no chat named"}),
    )
    allowed = (
        ("mute_chat", {"chat_id": "101"}),
        ("delete_contact", {"user_id": "101"}),
        ("block_user", {"user_id": 101}),
        ("subscribe_public_channel", {"channel": "@Team"}),
        # A user inside a named allowed chat is a member of that chat.
        ("ban_user", {"chat_id": "101", "user_id": "7"}),
    )
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        for index, (tool, args) in enumerate(denied):
            with pytest.raises(ToolError, match="chat_access_denied"):
                await client.call_tool(
                    "telegram_write", {"tool": tool, "arguments": args, "idempotency_key": f"d{index}"}
                )
        for index, (tool, args) in enumerate(allowed):
            await client.call_tool(
                "telegram_write", {"tool": tool, "arguments": args, "idempotency_key": f"a{index}"}
            )
    assert [call[0] for call in adapter.calls] == [tool for tool, _ in allowed]


async def test_denylist_applies_to_user_targets_of_generic_writes(config):
    surface = build_surface(denied_chats=["7"])
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        with pytest.raises(ToolError, match="chat_access_denied"):
            await client.call_tool("telegram_write", {"tool": "block_user", "arguments": {"user_id": "7"}})
        # Denylist-only mode keeps account-wide writes available (no allowlist to escape).
        await client.call_tool("telegram_write", {"tool": "update_profile", "arguments": {"about": "x"}})
    assert [call[0] for call in adapter.calls] == ["update_profile"]


async def test_denylist_filters_records(config):
    surface = build_surface(denied_chats=["101"])
    async with Client(
        create_server(config, transport="stdio", adapter=FixtureAdapter(), surface=surface)
    ) as c:
        listed = (await c.call_tool("telegram_list_chats", {})).structured_content
    assert "101" not in json.dumps(listed)


async def test_router_resources_and_prompts(config):
    server = create_server(
        config, transport="stdio", adapter=FixtureAdapter(), surface=build_surface("default")
    )
    assert len(server.instructions) <= 1300 and "telegram-safe-send" in server.instructions
    async with Client(server) as client:
        toolsets = json.loads((await client.read_resource("telegram://toolsets"))[0].text)
        assert (
            toolsets["toolsets"]["core"]["enabled"] and not toolsets["toolsets"]["advanced_write"]["enabled"]
        )
        errors = (await client.read_resource("telegram://guide/errors"))[0].text
        assert "chat_access_denied" in errors
        assert "telegram://guide/formatting" in (await client.read_resource("telegram://playbook"))[0].text
        prompts = {prompt.name for prompt in await client.list_prompts()}
        assert prompts == {"inbox_digest", "publish_post", "chat_research"}
        rendered = await client.get_prompt("publish_post", {"recipient": "@x", "brief": "hi"})
        assert "explicit yes" in rendered.messages[0].content.text


def test_descriptions_and_titles_cover_every_tool():
    assert set(TOOL_DESCRIPTIONS) == set(KNOWN_TOOLS)
    assert all(len(text) <= 300 for text in TOOL_DESCRIPTIONS.values())


def test_token_budget_and_skills():
    spec = importlib.util.spec_from_file_location("token_budget", ROOT / "scripts" / "token_budget.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0


async def test_opt_in_capabilities_keep_default_surface_flat(config):
    from fastmcp import Client

    from zai_telegram.server import create_server
    from zai_telegram.toolsets import build_surface

    opt_in = {"telegram_read", "telegram_poll_events", "telegram_add_account"}
    for preset in ("default", "all"):
        server = create_server(config, transport="stdio", adapter=object(), surface=build_surface(preset))
        async with Client(server) as client:
            assert not opt_in & {tool.name for tool in await client.list_tools()}, preset


def test_skill_references_mirror_server_guides():
    """Skills are self-contained copies; the server guides must say the same thing."""
    for path in ROOT.glob("skills/*/references/*.md"):
        if path.stem in GUIDE_TOPICS:
            assert path.read_text(encoding="utf-8") == guide(path.stem), path


def test_check_message_script():
    spec = importlib.util.spec_from_file_location(
        "check_message", ROOT / "skills" / "telegram-safe-send" / "scripts" / "check_message.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check("<b>ok</b> &amp;", html=True) == []
    assert len(module.check("<b>x & y", html=True)) == 2
    assert module.check("x" * 5000, html=False)


def test_vendor_upstream_bugs_are_fixed():
    """Static check: importing the vendor runtime needs live credentials."""
    from telethon import functions, types

    vendor = ROOT / "src" / "zai_telegram" / "_vendor" / "telegram_mcp" / "tools"
    media = (vendor / "media.py").read_text(encoding="utf-8")
    contacts = (vendor / "contacts.py").read_text(encoding="utf-8")
    assert not hasattr(functions.messages, "SearchGifsRequest")
    assert "SearchGifsRequest(" not in media and 'inline_query("gif", query)' in media
    assert hasattr(types, "InputPhoneContact") and not hasattr(functions.contacts, "InputPhoneContact")
    assert "functions.contacts.InputPhoneContact" not in contacts
    assert "types.InputPhoneContact(" in contacts


def test_compact_summary_mode_drops_empty_and_truncates():
    from zai_telegram.toolsets import compact

    payload = {"results": [{"id": 1, "message": "x" * 900, "reply_to": None, "entities": [1], "tags": []}]}
    out = compact(payload, 100)["results"][0]
    assert out["message_truncated"] and out["message_length"] == 900 and len(out["message"]) == 101
    assert "reply_to" not in out and "entities" not in out and "tags" not in out
    assert json.loads(compact(json.dumps(payload), 100))["results"][0]["message_truncated"]


async def test_summary_detail_applies_to_reads_only(config):
    surface = build_surface(detail="summary", max_text_chars=50)
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        chats = (await client.call_tool("telegram_list_chats", {})).structured_content
        sent = (await client.call_tool("telegram_send_message", SEND)).data
    assert "results" in json.dumps(chats) and sent["status"] == "sent"
    with pytest.raises(ValueError):
        build_surface(detail="tiny")


async def test_insight_tools_route_through_reviewed_allowlist(config):
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        await client.call_tool("telegram_list_topics", {"chat_id": "-100123", "limit": 10})
        await client.call_tool("telegram_get_admins", {"chat_id": "-100123"})
        await client.call_tool("telegram_get_chat_stats", {"chat_id": "@channel"})
    assert [call[0] for call in adapter.calls] == [
        "telegram_list_topics",
        "telegram_get_admins",
        "telegram_get_chat_stats",
    ]
    from zai_telegram.adapter import TelegramAdapter
    from zai_telegram.transport import ProviderError

    upstream, values = TelegramAdapter._upstream_call(
        "telegram_list_topics", {"chat_id": "-100123", "limit": 10, "account": "default"}
    )
    assert upstream == "list_topics" and values["chat_id"] == -100123
    with pytest.raises(ProviderError):
        TelegramAdapter._upstream_call(
            "telegram_list_topics", {"chat_id": "@x", "limit": 10, "account": "default"}
        )


def test_vendor_chat_stats_patch_uses_telethon_get_stats():
    from telethon import TelegramClient, functions

    chats = (ROOT / "src/zai_telegram/_vendor/telegram_mcp/tools/chats.py").read_text(encoding="utf-8")
    assert "async def get_chat_stats" in chats and "cl.get_stats(entity)" in chats
    assert hasattr(TelegramClient, "get_stats") and hasattr(functions.stats, "GetBroadcastStatsRequest")

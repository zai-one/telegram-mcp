"""Multi-account selection (backward compatible) and bounded wait_for_new_message. Mocks only."""

from dataclasses import replace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_polling import PollAdapter

import zai_telegram.polling as polling
from zai_telegram.config import ServiceConfig
from zai_telegram.server import create_server
from zai_telegram.toolsets import build_surface

WAIT = "telegram_wait_for_new_message"


def multi(config):
    return replace(config, bindings={"local-operator": ["default", "second"]})


async def schema(config, name):
    async with Client(create_server(config, transport="stdio", adapter=PollAdapter())) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    return tools, tools[name].input_schema["properties"]


async def test_single_account_schema_is_unchanged(config):
    tools, props = await schema(config, "telegram_get_chat")
    assert "account" not in props and "telegram_list_accounts" not in tools


async def test_multi_account_adds_optional_account_and_routes_calls(config):
    settings = multi(config)
    tools, props = await schema(settings, "telegram_get_chat")
    assert "account" in props and "account" not in tools["telegram_get_chat"].input_schema.get("required", [])
    assert "account" not in tools["telegram_check_update"].input_schema["properties"]
    adapter = PollAdapter()
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        listed = (await client.call_tool("telegram_list_accounts", {})).data
        assert listed == {"default": "default", "accounts": ["default", "second"]}
        await client.call_tool("telegram_get_chat", {"chat_id": "101"})
        await client.call_tool("telegram_get_chat", {"chat_id": "101", "account": "second"})
        await client.call_tool(
            "telegram_send_message",
            {"chat_id": "101", "message": "hi", "idempotency_key": "k1", "account": "second"},
        )
        with pytest.raises(ToolError):
            await client.call_tool("telegram_get_chat", {"chat_id": "101", "account": "stranger"})
    accounts = [args["account"] for _, args in adapter.calls]
    assert accounts == ["default", "second", "second"]


async def test_account_not_granted_to_other_principal(config):
    # "bob" may only use "second"; selecting "default" is refused.
    settings = replace(config, bindings={"local-operator": ["second"], "x": ["default", "second"]})
    adapter = PollAdapter()
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_get_chat", {"chat_id": "101", "account": "default"})
    assert adapter.calls == []


def test_binding_validation():
    with pytest.raises(ValueError):
        ServiceConfig(state_path="s", secret_path="p", bindings={"a": []})
    with pytest.raises(ValueError):
        ServiceConfig(state_path="s", secret_path="p", bindings={"a": ["x", "x"]})
    config = ServiceConfig(state_path="s", secret_path="p", bindings={"a": ["work", "home"]})
    assert config.bindings["a"] == "work" and config.multi_account


@pytest.fixture
def fast_wait(monkeypatch):
    monkeypatch.setattr(polling, "WAIT_MIN_INTERVAL", 0.01)
    calls = {"n": 0}

    async def sleep(seconds):
        calls["n"] += 1

    monkeypatch.setattr(polling, "_sleep", sleep)
    return calls


async def test_wait_returns_new_message_after_baseline(config, fast_wait, monkeypatch):
    adapter = PollAdapter()

    async def arrive(seconds):
        adapter.messages["101"] = [10, 11]

    monkeypatch.setattr(polling, "_sleep", arrive)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        result = (await client.call_tool(WAIT, {"chat_ids": ["101"], "timeout_seconds": 5})).data
    assert result["status"] == "new_messages"
    assert [row["message_id"] for row in result["reports"][0]["messages"]] == [11]
    assert result["batch_id"]


async def test_wait_times_out_and_validates_bounds(config, fast_wait, monkeypatch):
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(polling, "_monotonic", lambda: next(clock))
    async with Client(create_server(config, transport="stdio", adapter=PollAdapter())) as client:
        result = (await client.call_tool(WAIT, {"chat_ids": ["101"], "timeout_seconds": 7})).data
        assert result["status"] == "timeout"
        for bad in (0, 56):
            with pytest.raises(ToolError):
                await client.call_tool(WAIT, {"chat_ids": ["101"], "timeout_seconds": bad})


async def test_wait_respects_chat_allowlist(config):
    surface = build_surface("all", allowed_chats=["202"])
    adapter = PollAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        with pytest.raises(ToolError):
            await client.call_tool(WAIT, {"chat_ids": ["101"], "timeout_seconds": 1})
    assert adapter.calls == []

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from zai_telegram.adapter import TelegramAdapter
from zai_telegram.runtime import Runtime
from zai_telegram.server import create_server
from zai_telegram.state import StateStore
from zai_telegram.transport import ProviderTimeoutError, ProviderTransportError


class FixtureAdapter:
    risk_tier = staticmethod(TelegramAdapter.risk_tier)
    validate_write = TelegramAdapter.validate_write
    _bounded_text = staticmethod(TelegramAdapter._bounded_text)
    write_enabled = True

    def __init__(self):
        self.calls = []
        self.failure = None

    def has_account(self, label):
        return label in {"default", "second"}

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if self.failure:
            raise self.failure
        if tool == "telegram_get_chat":
            return {"id": args["chat_id"], "title": "Fixture", "last_message": "foreign"}
        return {"results": [{"id": 101, "name": "Fixture", "message": "synthetic-api-hash"}]}

    async def call_write(self, tool, args):
        self.calls.append((tool, args))
        if self.failure:
            raise self.failure
        return {"message_id": len(self.calls), "chat_id": args.get("chat_id"), "status": "sent"}


SEND = {"chat_id": "101", "message": "synthetic content", "idempotency_key": "stable"}


async def test_successful_write_replays_after_restart_and_does_not_leak_body_to_audit(config):
    adapter = FixtureAdapter()
    for _ in range(2):
        async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
            result = await client.call_tool("telegram_send_message", SEND)
            assert result.data["message_id"] == 1
    assert len(adapter.calls) == 1
    with StateStore(config).connect() as db:
        audit = [dict(row) for row in db.execute("SELECT * FROM telegram_audit")]
        assert len(audit) == 2 and all(row["outcome"] == "success" for row in audit)
        assert "synthetic content" not in json.dumps(audit)


async def test_changed_payload_and_account_cannot_reuse_old_idempotency_result(config):
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        await client.call_tool("telegram_send_message", SEND)
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", {**SEND, "message": "changed"})
    rebound = replace(config, bindings={"local-operator": "second"})
    async with Client(create_server(rebound, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", SEND)
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("failure", [ProviderTimeoutError("echo"), ProviderTransportError("echo")])
async def test_unknown_send_survives_restart_and_new_key_does_not_resubmit(config, failure):
    adapter = FixtureAdapter()
    adapter.failure = failure
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", SEND)
    adapter.failure = None
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        for key in ["stable", "new-key"]:
            with pytest.raises(ToolError):
                await client.call_tool("telegram_send_message", {**SEND, "idempotency_key": key})
        rows = (await client.call_tool("telegram_outbox_status", {})).data["records"]
        assert len(rows) == 1 and rows[0]["reconciliation_required"]
        assert rows[0]["status"] == "pending" and rows[0]["message_ids"] == []
    assert len(adapter.calls) == 1


async def test_pause_write_switch_bad_input_and_unknown_account_have_no_dispatch_or_ledger(config):
    adapter = FixtureAdapter()
    for settings in [
        replace(config, enabled=False),
        replace(config, telegram_write_enabled=False),
        replace(config, bindings={"local-operator": "missing"}),
    ]:
        async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
            with pytest.raises(ToolError):
                await client.call_tool("telegram_send_message", SEND)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_write", {"tool": "unreviewed_tool", "arguments": {}})
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", {**SEND, "message": ""})
    assert adapter.calls == []
    assert await StateStore(config).telegram_write_list("local-operator") == []


async def test_client_account_override_cannot_escape_binding_and_result_is_sanitized(config):
    adapter = FixtureAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        await client.call_tool(
            "telegram_write",
            {
                "tool": "send_message",
                "arguments": {"chat_id": "101", "message": "fixture", "account": "second"},
                "idempotency_key": "override",
            },
        )
        result = (await client.call_tool("telegram_list_chats", {})).data
    assert all(args["account"] == "default" for _, args in adapter.calls)
    assert "synthetic-api-hash" not in json.dumps(result)


async def test_rate_high_risk_and_cooldown_persist_across_instances(config):
    adapter = FixtureAdapter()
    limited = replace(config, rate_limit=1)
    for index in range(2):
        async with Client(create_server(limited, transport="stdio", adapter=adapter)) as client:
            if not index:
                await client.call_tool("telegram_list_chats", {})
            else:
                with pytest.raises(ToolError, match="provider_rate_limited"):
                    await client.call_tool("telegram_list_chats", {})
    assert len(adapter.calls) == 1
    store = StateStore(config)
    for _ in range(3):
        assert await store.consume_provider_request("alice", "telegram:high", 3)
    assert not await StateStore(config).consume_provider_request("local-operator", "telegram:high", 999)
    assert await StateStore(config).consume_provider_request("bob", "telegram:high", 3)


async def test_batch_validation_precedes_first_write_and_replay_is_per_item(config):
    adapter = FixtureAdapter()
    item = {"item_id": "one", "recipient_ref": "telegram:101", "message": "fixture", "idempotency_key": "one"}
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_many", {"batch_id": "batch", "items": [item, item]})
        assert adapter.calls == []
        first = (await client.call_tool("telegram_send_many", {"batch_id": "batch", "items": [item]})).data
        assert first["summary"] == {"sent": 1}
        replay = (await client.call_tool("telegram_send_many", {"batch_id": "batch", "items": [item]})).data
        assert replay["summary"] == {"duplicate_replay": 1}
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("deadline", [False, True])
async def test_cancel_and_deadline_keep_pending_without_detached_send(config, monkeypatch, deadline):
    entered, released = asyncio.Event(), asyncio.Event()
    adapter = FixtureAdapter()

    async def slow(tool, args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    adapter.call_write = slow
    runtime = Runtime(config, "stdio", adapter)
    from zai_telegram.tools import register_tools

    functions = {}

    class Registrar:
        def tool(self, **kwargs):
            def save(fn):
                functions[fn.__name__] = fn
                return fn

            return save

    register_tools(Registrar(), runtime)
    if deadline:
        monkeypatch.setattr("zai_telegram.runtime.OPERATION_TIMEOUT_SECONDS", 0.03)
    task = asyncio.create_task(
        runtime.execute("telegram_send_message", SEND, lambda: functions["telegram_send_message"](**SEND))
    )
    await entered.wait()
    if not deadline:
        task.cancel()
    with pytest.raises(ToolError if deadline else asyncio.CancelledError):
        await task
    assert released.is_set()
    rows = await StateStore(config).telegram_write_list("local-operator")
    assert rows[0]["status"] == "pending"
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_leases").fetchone()[0] == 0

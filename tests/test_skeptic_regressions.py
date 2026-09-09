from dataclasses import replace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import SEND, FixtureAdapter

from zai_telegram.server import create_server
from zai_telegram.state import StateStore


async def test_known_local_quota_denial_can_be_retried_with_new_key_after_window(config):
    settings = replace(config, write_rate_limit=1)
    adapter = FixtureAdapter()
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        await client.call_tool(
            "telegram_send_message", {**SEND, "message": "first", "idempotency_key": "first"}
        )
        with pytest.raises(ToolError, match="provider_rate_limited"):
            await client.call_tool("telegram_send_message", SEND)
    assert len(adapter.calls) == 1
    store = StateStore(settings)
    rows = await store.telegram_write_list("local-operator", idempotency_keys=["stable"])
    assert rows[0]["status"] == "failed"
    # Move only synthetic admission timestamps outside the minute window.
    with store.connect() as db:
        db.execute("UPDATE telegram_attempts SET started=started-61")
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        result = await client.call_tool(
            "telegram_send_message", {**SEND, "idempotency_key": "retry-after-quota"}
        )
        assert result.data["message_id"] == 2
    assert len(adapter.calls) == 2

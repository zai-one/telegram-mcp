"""Debounced telegram_wait_for_settled_message. Mocks only — no real Telegram."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_polling import PollAdapter

import zai_telegram.polling as polling
from zai_telegram.server import create_server
from zai_telegram.toolsets import build_surface

SETTLED = "telegram_wait_for_settled_message"


@pytest.fixture
def fast_wait(monkeypatch):
    monkeypatch.setattr(polling, "WAIT_MIN_INTERVAL", 0.01)

    async def sleep(_seconds):
        return None

    monkeypatch.setattr(polling, "_sleep", sleep)


async def test_settled_returns_after_quiet_period(config, fast_wait, monkeypatch):
    adapter = PollAdapter()
    times = [0.0]
    sleeps = {"n": 0}

    def mono():
        return times[0]

    async def sleep(seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            # First sleep (waiting for activity): messages arrive.
            adapter.messages["101"] = [10, 11, 12]
            times[0] += 1.0
        else:
            # Settle sleep: advance by the requested quiet window.
            times[0] += seconds

    monkeypatch.setattr(polling, "_monotonic", mono)
    monkeypatch.setattr(polling, "_sleep", sleep)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        result = (
            await client.call_tool(
                SETTLED,
                {"chat_ids": ["101"], "settle_seconds": 6, "max_wait_seconds": 40},
            )
        ).data
    assert result["status"] == "settled"
    assert result["mode"] == "wait_settled"
    assert result["settle_seconds"] == 6
    assert result["message_count"] == 2
    assert [row["message_id"] for row in result["reports"][0]["messages"]] == [11, 12]
    assert result["batch_id"]


async def test_settled_timeout_with_no_messages(config, fast_wait, monkeypatch):
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(polling, "_monotonic", lambda: next(clock))
    async with Client(create_server(config, transport="stdio", adapter=PollAdapter())) as client:
        result = (
            await client.call_tool(
                SETTLED,
                {"chat_ids": ["101"], "settle_seconds": 2, "max_wait_seconds": 7},
            )
        ).data
        assert result["status"] == "timeout"
        assert result["message_count"] == 0
        for bad_settle, bad_wait in ((0, 10), (31, 10), (6, 0), (6, 56)):
            with pytest.raises(ToolError):
                await client.call_tool(
                    SETTLED,
                    {
                        "chat_ids": ["101"],
                        "settle_seconds": bad_settle,
                        "max_wait_seconds": bad_wait,
                    },
                )


async def test_settled_partial_when_deadline_hits_mid_burst(config, fast_wait, monkeypatch):
    adapter = PollAdapter()
    state = {"n": 10}
    times = [0.0]

    def mono():
        return times[0]

    async def keep_arriving(seconds):
        state["n"] += 1
        adapter.messages["101"] = list(range(10, state["n"] + 1))
        # Always advance enough that settle never completes before max_wait.
        times[0] += max(seconds, 3.0)

    monkeypatch.setattr(polling, "_monotonic", mono)
    monkeypatch.setattr(polling, "_sleep", keep_arriving)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        result = (
            await client.call_tool(
                SETTLED,
                {"chat_ids": ["101"], "settle_seconds": 30, "max_wait_seconds": 8},
            )
        ).data
    assert result["status"] == "partial"
    assert result["message_count"] >= 1
    assert result["reports"] and result["reports"][0]["messages"]
    assert result["batch_id"]


async def test_settled_respects_chat_allowlist(config):
    surface = build_surface("all", allowed_chats=["202"])
    adapter = PollAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                SETTLED,
                {"chat_ids": ["101"], "settle_seconds": 1, "max_wait_seconds": 1},
            )
    assert adapter.calls == []

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


async def test_settled_keeps_messages_of_watched_chats_when_another_chat_is_new(config, monkeypatch):
    """Regression: baselining a new chat must not acknowledge a watched chat's unread messages."""
    adapter = PollAdapter()
    times = [0.0]
    monkeypatch.setattr(polling, "_monotonic", lambda: times[0])

    async def sleep(seconds):
        times[0] += seconds

    monkeypatch.setattr(polling, "_sleep", sleep)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        baseline = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})).data
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": baseline["batch_id"]})
        adapter.messages["101"] = [10, 11, 12]  # two unread replies in the watched chat
        result = (
            await client.call_tool(
                SETTLED, {"chat_ids": ["101", "202"], "settle_seconds": 2, "max_wait_seconds": 30}
            )
        ).data
        assert result["status"] == "settled" and result["message_count"] == 2
        assert [report["chat_id"] for report in result["reports"]] == ["101"]
        assert [row["message_id"] for row in result["reports"][0]["messages"]] == [11, 12]
        # Not acknowledged yet: a poll still returns them; 202 now has a baseline.
        again = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101", "202"]})).data
        by_chat = {report["chat_id"]: report for report in again["reports"]}
        assert [row["message_id"] for row in by_chat["101"]["messages"]] == [11, 12]
        assert by_chat["202"]["baseline_only"] is False
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": result["batch_id"]})
        after = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})).data
        assert after["reports"][0]["messages"] == []


async def test_settled_returns_watched_messages_even_if_deadline_hits_first(config, monkeypatch):
    adapter = PollAdapter()
    times = [0.0]
    monkeypatch.setattr(polling, "_monotonic", lambda: times[0])

    async def sleep(seconds):
        times[0] += 100.0  # deadline passes during the first sleep

    monkeypatch.setattr(polling, "_sleep", sleep)
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        baseline = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})).data
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": baseline["batch_id"]})
        adapter.messages["101"] = [10, 11]
        result = (
            await client.call_tool(
                SETTLED, {"chat_ids": ["101", "202"], "settle_seconds": 6, "max_wait_seconds": 5}
            )
        ).data
        assert result["status"] == "partial"
        assert [row["message_id"] for row in result["reports"][0]["messages"]] == [11]
        # The returned batch is acknowledgeable (its proposal excludes the new chat's baseline).
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": result["batch_id"]})
        after = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101", "202"]})).data
    assert all(report["messages"] == [] and not report["baseline_only"] for report in after["reports"])


async def test_settled_paces_polls_like_wait_for_new_message(config, monkeypatch):
    """Regression: the settle phase must not re-poll every settle_seconds x chats."""
    monkeypatch.setattr(polling, "WAIT_MIN_INTERVAL", 0.01)
    adapter = PollAdapter()
    times = [0.0]
    poll_times: dict[float, int] = {}
    monkeypatch.setattr(polling, "_monotonic", lambda: times[0])

    async def sleep(seconds):
        times[0] += seconds

    original = adapter.poll_messages

    async def poll_messages(chat, after, limit, account):
        poll_times[times[0]] = poll_times.get(times[0], 0) + 1
        if after is not None and len(poll_times) <= 4:
            # A message burst that keeps growing for the first few polls.
            adapter.messages[chat] = [*adapter.messages[chat], adapter.messages[chat][-1] + 1]
        return await original(chat, after, limit, account)

    adapter.poll_messages = poll_messages
    monkeypatch.setattr(polling, "_sleep", sleep)
    chats = ["101", "202"]
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        baseline = (await client.call_tool("telegram_poll_messages", {"chat_ids": chats})).data
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": baseline["batch_id"]})
        poll_times.clear()
        result = (
            await client.call_tool(SETTLED, {"chat_ids": chats, "settle_seconds": 1, "max_wait_seconds": 55})
        ).data
    assert result["status"] == "settled"
    moments = sorted(poll_times)
    interval = 2.5 * len(chats)
    assert all(later - earlier >= interval for earlier, later in zip(moments, moments[1:], strict=False))
    # Several polls ran (burst + confirm), each reading every chat once: 2 reads per 5 s = 24/min.
    assert len(moments) >= 4 and set(poll_times.values()) == {len(chats)}

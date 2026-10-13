"""Event feed (new/edited/deleted) via Telethon handlers: cursor, gap, ACL, multi-account. Mocks only."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram.server import create_server
from zai_telegram.toolsets import build_surface

POLL = "telegram_poll_events"


class FakeClient:
    def __init__(self):
        self.handlers = []

    def add_event_handler(self, callback, event):
        self.handlers.append((type(event).__name__, callback))

    def is_connected(self):
        return True

    async def emit(self, kind, **fields):
        name = {"new": "NewMessage", "edited": "MessageEdited", "deleted": "MessageDeleted"}[kind]
        for event_name, callback in self.handlers:
            if event_name == name:
                if kind == "deleted":
                    await callback(SimpleNamespace(chat_id=fields.get("chat_id"), deleted_ids=fields["ids"]))
                else:
                    message = SimpleNamespace(
                        chat_id=fields["chat_id"],
                        id=fields["id"],
                        sender_id=7,
                        message=fields.get("text", "hi"),
                        out=False,
                        media=None,
                        date=None,
                        edit_date=None,
                    )
                    await callback(SimpleNamespace(message=message))


class FeedAdapter(FixtureAdapter):
    def __init__(self):
        super().__init__()
        self.clients = {"default": FakeClient(), "second": FakeClient()}

    def event_client(self, account):
        return self.clients[account]


def server(config, adapter, **surface):
    return create_server(
        config, transport="stdio", adapter=adapter, surface=build_surface("default,event_feed", **surface)
    )


async def test_opt_in_and_default_surface_unchanged(config):
    async with Client(create_server(config, transport="stdio", adapter=FeedAdapter())) as client:
        assert POLL not in {t.name for t in await client.list_tools()}


async def test_new_edit_delete_cursor_filters_and_has_more(config):
    adapter = FeedAdapter()
    fake = adapter.clients["default"]
    async with Client(server(config, adapter)) as client:
        started = (await client.call_tool(POLL, {})).data
        assert started["status"] == "started" and started["events"] == []
        await fake.emit("new", chat_id=101, id=1, text="synthetic")
        await fake.emit("edited", chat_id=101, id=1, text="synthetic edited")
        await fake.emit("deleted", chat_id=-1001, ids=[5, 6])
        await fake.emit("new", chat_id=202, id=9)
        page = (await client.call_tool(POLL, {"cursor": started["next_cursor"], "limit": 2})).data
        assert [e["kind"] for e in page["events"]] == ["new", "edited"] and page["has_more"]
        assert page["events"][0]["untrusted_content"] is True
        rest = (await client.call_tool(POLL, {"cursor": page["next_cursor"]})).data
        assert [e["kind"] for e in rest["events"]] == ["deleted", "new"] and not rest["has_more"]
        assert rest["events"][0]["message_ids"] == [5, 6]
        only = (
            await client.call_tool(
                POLL, {"cursor": started["next_cursor"], "chat_ids": ["202"], "kinds": ["new"]}
            )
        ).data
        assert [(e["chat_id"], e["message_id"]) for e in only["events"]] == [(202, 9)]
        assert only["next_cursor"] == rest["next_cursor"]  # filtered events are skipped, not replayed
        empty = (await client.call_tool(POLL, {"cursor": rest["next_cursor"]})).data
        assert empty["status"] == "empty" and not empty["gap"]


async def test_wait_returns_on_event(config):
    adapter = FeedAdapter()
    fake = adapter.clients["default"]
    async with Client(server(config, adapter)) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]

        async def later():
            await asyncio.sleep(0.05)
            await fake.emit("new", chat_id=101, id=3)

        task = asyncio.create_task(later())
        got = (await client.call_tool(POLL, {"cursor": cursor, "wait_seconds": 5})).data
        await task
        assert [e["message_id"] for e in got["events"]] == [3]


async def test_acl_filters_events_and_hides_chatless_deletions(config):
    adapter = FeedAdapter()
    fake = adapter.clients["default"]
    async with Client(server(config, adapter, allowed_chats=["101"])) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]
        await fake.emit("new", chat_id=202, id=1)
        await fake.emit("deleted", chat_id=None, ids=[1])
        await fake.emit("new", chat_id=101, id=2)
        got = (await client.call_tool(POLL, {"cursor": cursor})).data
        assert [(e["chat_id"], e["message_id"]) for e in got["events"]] == [(101, 2)]
        with pytest.raises(ToolError, match="chat_access_denied"):
            await client.call_tool(POLL, {"cursor": cursor, "chat_ids": ["202"]})


async def test_multi_account_feeds_are_separate_and_cursor_bound(config):
    settings = replace(config, bindings={"local-operator": ["default", "second"]})
    adapter = FeedAdapter()
    async with Client(server(settings, adapter)) as client:
        first = (await client.call_tool(POLL, {})).data["next_cursor"]
        second = (await client.call_tool(POLL, {"account": "second"})).data["next_cursor"]
        await adapter.clients["second"].emit("new", chat_id=55, id=1)
        assert (await client.call_tool(POLL, {"cursor": first})).data["events"] == []
        got = (await client.call_tool(POLL, {"cursor": second, "account": "second"})).data
        assert got["account"] == "second" and len(got["events"]) == 1
        with pytest.raises(ToolError):
            await client.call_tool(POLL, {"cursor": second})  # cursor names another account
        with pytest.raises(ToolError):
            await client.call_tool(POLL, {"account": "third"})


async def test_gap_after_restart_or_overflow_and_bad_cursor(config, monkeypatch):
    import zai_telegram.feed as feed

    adapter = FeedAdapter()
    async with Client(server(config, adapter)) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]
    adapter.clients["default"] = FakeClient()
    async with Client(server(config, adapter)) as client:
        await client.call_tool(POLL, {})
        assert (await client.call_tool(POLL, {"cursor": cursor})).data["gap"] is True
        for bad in ("nope", "default.zzzzzzzz.1"):
            with pytest.raises(ToolError):
                await client.call_tool(POLL, {"cursor": bad})
    monkeypatch.setattr(feed, "RING_SIZE", 2)
    adapter.clients["default"] = FakeClient()
    async with Client(server(config, adapter)) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]
        for i in range(1, 5):
            await adapter.clients["default"].emit("new", chat_id=101, id=i)
        got = (await client.call_tool(POLL, {"cursor": cursor})).data
        assert got["gap"] is True and [e["message_id"] for e in got["events"]] == [3, 4]


async def test_restart_keeps_cursor_and_ids_only_events_with_bounded_gap_window(config):
    from zai_telegram.state import StateStore

    adapter = FeedAdapter()
    async with Client(server(config, adapter)) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]
        await adapter.clients["default"].emit("new", chat_id=101, id=1, text="synthetic-secret-body")
    adapter.clients["default"] = FakeClient()  # restart: new process, new handlers
    async with Client(server(config, adapter)) as client:
        await client.call_tool(POLL, {})
        await adapter.clients["default"].emit("new", chat_id=101, id=2, text="after")
        got = (await client.call_tool(POLL, {"cursor": cursor})).data
        assert [e["message_id"] for e in got["events"]] == [1, 2]
        assert got["events"][0]["text_unavailable"] and got["events"][1]["text"] == "after"
        assert got["gap"] and set(got["gap_window"]) == {"from", "to"}
        fresh = (await client.call_tool(POLL, {"cursor": got["next_cursor"]})).data
        assert not fresh["gap"] and "gap_window" not in fresh
    with StateStore(config).connect() as db:
        stored = str([tuple(r) for r in db.execute("SELECT * FROM telegram_feed_events")])
    assert "synthetic-secret-body" not in stored and "after" not in stored


async def test_persistence_can_be_disabled(config, monkeypatch):
    monkeypatch.setenv("TELEGRAM_FEED_PERSIST", "0")
    adapter = FeedAdapter()
    async with Client(server(config, adapter)) as client:
        cursor = (await client.call_tool(POLL, {})).data["next_cursor"]
    adapter.clients["default"] = FakeClient()
    async with Client(server(config, adapter)) as client:
        await client.call_tool(POLL, {})
        got = (await client.call_tool(POLL, {"cursor": cursor})).data
        assert got["gap"] and "gap_window" not in got

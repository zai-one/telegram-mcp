"""Native parity reads/writes: gated by telegram_read / telegram_write, ACL and categories. Mocks only."""

from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram import native
from zai_telegram.adapter import TELEGRAM_READ_ALLOWLIST, TELEGRAM_WRITE_ALLOWLIST, TelegramAdapter
from zai_telegram.server import create_server
from zai_telegram.toolsets import WRITE_CATEGORIES, build_surface
from zai_telegram.transport import ProviderError


def test_every_native_tool_is_on_an_allowlist_and_in_a_category():
    assert set(native.NATIVE_READS) <= TELEGRAM_READ_ALLOWLIST
    categorized = {tool for tools in WRITE_CATEGORIES.values() for tool in tools}
    for name, (_, tier, category) in native.NATIVE_WRITES.items():
        assert TELEGRAM_WRITE_ALLOWLIST[name] == tier and name in WRITE_CATEGORIES[category]
    assert set(TELEGRAM_WRITE_ALLOWLIST) <= categorized


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/durov/123", ("durov", 123)),
        ("t.me/c/1234567/42", (-1001234567, 42)),
        ("https://t.me/c/1234567/9/42?single", (-1001234567, 42)),
        ("https://t.me/s/durov/77", ("durov", 77)),
    ],
)
def test_message_link_parsing(link, expected):
    assert native.parse_message_link(link) == expected


@pytest.mark.parametrize("bad", ["https://evil.example/durov/1", "t.me/durov", 5, "x" * 300])
def test_message_link_rejects_other_hosts(bad):
    with pytest.raises(ProviderError):
        native.parse_message_link(bad)


class PollClient:
    def __init__(self, multiple=False):
        answers = [SimpleNamespace(option=b"a"), SimpleNamespace(option=b"b")]
        self.poll = SimpleNamespace(answers=answers, multiple_choice=multiple)
        self.sent = []

    async def get_input_entity(self, chat):
        return chat

    async def get_messages(self, entity, ids):
        return SimpleNamespace(media=SimpleNamespace(poll=self.poll))

    async def __call__(self, request):
        self.sent.append(request)


async def test_vote_poll_maps_indexes_and_respects_single_choice():
    client = PollClient()
    result = await native.vote_poll(client, {"chat_id": "101", "message_id": 5, "option_indexes": [1]})
    assert result["status"] == "voted" and client.sent[0].options == [b"b"]
    for bad in ([0, 1], [2], []):
        with pytest.raises(ProviderError):
            await native.vote_poll(client, {"chat_id": "101", "message_id": 5, "option_indexes": bad})


async def test_generic_gates_route_native_names(config):
    adapter = FixtureAdapter()
    surface = build_surface("all,advanced_read", allowed_chats=["101", "7"])
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        await client.call_tool(
            "telegram_read",
            {"tool": "get_member_admin_status", "arguments": {"chat_id": "101", "user_id": "7"}},
        )
        for bad in (
            {"tool": "export_unread_messages", "arguments": {}},
            {"tool": "get_message_by_link", "arguments": {"link": "https://t.me/x_chat/1"}},
        ):
            with pytest.raises(ToolError, match="chat_access_denied"):
                await client.call_tool("telegram_read", bad)
        await client.call_tool(
            "telegram_write",
            {"tool": "vote_poll", "arguments": {"chat_id": "101", "message_id": 5, "option_indexes": [0]}},
        )
        with pytest.raises(ToolError, match="chat_access_denied"):
            await client.call_tool(
                "telegram_write", {"tool": "rename_folder", "arguments": {"folder_id": 3, "title": "x"}}
            )
    assert [c[0] for c in adapter.calls] == ["telegram_read", "vote_poll"]


async def test_write_category_gate_applies_to_native_writes(config):
    adapter = FixtureAdapter()
    surface = build_surface("default,advanced_write,messages_write")
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        with pytest.raises(ToolError, match="tool_disabled"):
            await client.call_tool(
                "telegram_write", {"tool": "remove_user", "arguments": {"chat_id": "1", "user_id": "2"}}
            )


async def test_adapter_dispatches_native_without_upstream_server():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    client = PollClient()
    adapter._runtime = SimpleNamespace(get_client=lambda label: client)
    result = await adapter._native(
        native.vote_poll, {"account": "default", "chat_id": "1", "message_id": 2, "option_indexes": [0]}
    )
    assert result["status"] == "voted"


async def test_native_calls_run_the_runtime_liveness_check_first():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    client, order = PollClient(), []

    async def ensure(cl):
        order.append(("ensure", cl))

    adapter._runtime = SimpleNamespace(get_client=lambda label: client, ensure_connected=ensure)
    await adapter._native(
        native.vote_poll, {"account": "default", "chat_id": "1", "message_id": 2, "option_indexes": [0]}
    )
    assert order == [("ensure", client)] and len(client.sent) == 1


async def test_vote_poll_rejects_repeated_indexes_and_closed_polls():
    client = PollClient(multiple=True)
    with pytest.raises(ProviderError, match="repeat"):
        await native.vote_poll(client, {"chat_id": "1", "message_id": 5, "option_indexes": [0, 0]})
    client.poll.closed = True
    with pytest.raises(ProviderError, match="closed"):
        await native.vote_poll(client, {"chat_id": "1", "message_id": 5, "option_indexes": [0]})
    assert client.sent == []


class KickClient:
    def __init__(self):
        self.kicked = []

    async def get_entity(self, value):
        return SimpleNamespace(id=777 if value in {"me", 777} else int(value))

    async def get_me(self, input_peer=False):
        return SimpleNamespace(user_id=777)

    async def kick_participant(self, entity, user):
        self.kicked.append(user.id)


@pytest.mark.parametrize("own", ["me", "@Me", "self", 777])
async def test_remove_user_never_removes_the_account_itself(own):
    # kick_participant(chat, "me") would make the account leave the chat.
    client = KickClient()
    with pytest.raises(ProviderError, match="itself"):
        await native.remove_user(client, {"chat_id": "-100123", "user_id": own})
    assert client.kicked == []
    await native.remove_user(client, {"chat_id": "-100123", "user_id": "42"})
    assert client.kicked == [42]


async def test_message_by_link_is_allowed_for_an_allowlisted_chat(config):
    adapter = FixtureAdapter()
    surface = build_surface("all,advanced_read", allowed_chats=["okchan", "-1001234567"])
    async with Client(create_server(config, transport="stdio", adapter=adapter, surface=surface)) as client:
        for link in ("https://t.me/s/okchan/5", "t.me/c/1234567/9"):
            await client.call_tool(
                "telegram_read", {"tool": "get_message_by_link", "arguments": {"link": link}}
            )
        for link in ("https://t.me/other/5", "t.me/c/7654321/9", "https://evil.example/okchan/5"):
            with pytest.raises(ToolError, match="chat_access_denied"):
                await client.call_tool(
                    "telegram_read", {"tool": "get_message_by_link", "arguments": {"link": link}}
                )
    assert len(adapter.calls) == 2


class TranscribeClient:
    def __init__(self, pending=True, final=True):
        self.handlers, self.pending, self.final = [], pending, final

    async def get_input_entity(self, chat):
        return chat

    def add_event_handler(self, cb, event):
        self.handlers.append(cb)

    def remove_event_handler(self, cb, event):
        self.handlers.remove(cb)

    async def __call__(self, request):
        import asyncio

        from telethon.tl import types

        if self.pending and self.final:
            update = types.UpdateTranscribedAudio(
                peer=types.PeerUser(1), msg_id=request.msg_id, transcription_id=9, text="hello world"
            )
            other = types.UpdateTranscribedAudio(
                peer=types.PeerUser(1), msg_id=request.msg_id, transcription_id=8, text="wrong"
            )
            loop = asyncio.get_running_loop()
            for u in (other, update):
                loop.call_later(0.05, lambda u=u: [asyncio.ensure_future(h(u)) for h in list(self.handlers)])
        return SimpleNamespace(pending=self.pending, transcription_id=9, text="hel", trial_remains_num=None)


async def test_transcribe_waits_for_the_final_update():
    client = TranscribeClient()
    result = await native.transcribe_voice(client, {"chat_id": "1", "message_id": 3, "wait_seconds": 2})
    assert result["text"] == "hello world" and result["pending"] is False and not client.handlers


async def test_transcribe_times_out_and_returns_partial():
    client = TranscribeClient(final=False)
    result = await native.transcribe_voice(client, {"chat_id": "1", "message_id": 3, "wait_seconds": 0})
    assert result["pending"] is True and result["text"] == "hel" and not client.handlers
    with pytest.raises(ProviderError):
        await native.transcribe_voice(client, {"chat_id": "1", "message_id": 3, "wait_seconds": 99})


async def test_rename_folder_handles_shared_chatlists():
    from telethon.tl import types

    shared = types.DialogFilterChatlist(
        id=4, title=types.TextWithEntities("Old", []), pinned_peers=[], include_peers=[types.InputPeerSelf()]
    )

    class FolderClient:
        sent = []

        async def __call__(self, request):
            if type(request).__name__ == "GetDialogFiltersRequest":
                return SimpleNamespace(filters=[shared])
            self.sent.append(request)
            return True

    client = FolderClient()
    await native.rename_folder(client, {"folder_id": 4, "title": "New"})
    sent = client.sent[0]
    assert sent.id == 4 and isinstance(sent.filter, types.DialogFilterChatlist)
    assert sent.filter.title.text == "New"


class TopicClient:
    def __init__(self, offsets):
        self.offsets, self.sent = list(offsets), []

    async def get_input_entity(self, chat):
        return chat

    async def __call__(self, request):
        self.sent.append(request)
        return SimpleNamespace(pts=1, pts_count=1, offset=self.offsets.pop(0) if self.offsets else 0)


async def test_delete_forum_topic_repeats_until_offset_is_zero():
    """Regression: affectedHistory.offset > 0 means the topic is only partly deleted."""
    client = TopicClient([100, 50, 0])
    result = await native.delete_forum_topic(client, {"chat_id": -1001, "topic_id": 5})
    assert result["status"] == "deleted" and len(client.sent) == 3


async def test_delete_forum_topic_reports_partial_after_batch_cap(monkeypatch):
    monkeypatch.setattr(native, "DELETE_TOPIC_BATCHES", 2)
    client = TopicClient([100, 100, 100])
    result = await native.delete_forum_topic(client, {"chat_id": -1001, "topic_id": 5})
    assert result["status"] == "partially_deleted" and len(client.sent) == 2


async def test_edit_forum_topic_sends_stripped_title():
    client = TopicClient([])
    await native.edit_forum_topic(client, {"chat_id": -1001, "topic_id": 5, "title": "  News  "})
    assert client.sent[0].title == "News"

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

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_runtime import FixtureAdapter

from zai_telegram.polling import PollStore, read_since
from zai_telegram.server import create_server
from zai_telegram.state import StateStore
from zai_telegram.transport import ProviderError


class PollAdapter(FixtureAdapter):
    def __init__(self):
        super().__init__()
        self.messages = {"101": [10], "202": [20]}

    async def poll_messages(self, chat, after, limit, account):
        self.calls.append((chat, after, limit, account))
        if self.failure:
            raise self.failure
        ids = (
            self.messages[chat][-1:]
            if after is None
            else [i for i in self.messages[chat] if i > after][: limit + 1]
        )
        return [
            {"message_id": i, "text": "synthetic-api-hash message", "untrusted_content": True} for i in ids
        ]


async def test_poll_baseline_explicit_ack_restart_bounded_catchup_and_no_bodies_in_state(config):
    adapter = PollAdapter()
    settings = replace(config, rate_limit=30)
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        baseline = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})).data
        assert baseline["reports"][0]["baseline_only"] and baseline["reports"][0]["messages"] == []
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": baseline["batch_id"]})
    adapter.messages["101"] = [10, 11, 12, 13]
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        args = {"chat_ids": ["101"], "max_per_chat": 2}
        first = (await client.call_tool("telegram_poll_messages", args)).data
        repeated = (await client.call_tool("telegram_poll_messages", args)).data
        assert [row["message_id"] for row in first["reports"][0]["messages"]] == [11, 12]
        assert first["reports"][0]["has_more"] and first["reports"] == repeated["reports"]
        assert "synthetic-api-hash" not in json.dumps(first)
        await client.call_tool("telegram_acknowledge_poll", {"batch_id": first["batch_id"]})
        replay = (await client.call_tool("telegram_acknowledge_poll", {"batch_id": first["batch_id"]})).data
        assert replay["replayed"]
        with pytest.raises(ToolError):
            await client.call_tool("telegram_acknowledge_poll", {"batch_id": repeated["batch_id"]})
        last = (await client.call_tool("telegram_poll_messages", args)).data
        assert [row["message_id"] for row in last["reports"][0]["messages"]] == [13]
        assert not last["reports"][0]["has_more"]
    with StateStore(config).connect() as db:
        stored = str([tuple(row) for row in db.execute("SELECT * FROM telegram_poll_batches")])
    assert "message" not in stored and "synthetic-api-hash" not in stored


def test_poll_batch_owner_account_binding_expiry_and_atomic_conflict(config):
    store = PollStore(StateStore(config))
    batch = store.prepare("alice", {"101": [None, 5], "202": [None, 7]})
    with pytest.raises(ProviderError):
        store.acknowledge("bob", batch)
    changed = PollStore(StateStore(replace(config, bindings={**dict(config.bindings), "alice": "second"})))
    with pytest.raises(ProviderError):
        changed.acknowledge("alice", batch)
    other = store.prepare("alice", {"202": [None, 8]})
    store.acknowledge("alice", other)
    with pytest.raises(ProviderError):
        store.acknowledge("alice", batch)
    assert store.cursors("alice", ["101", "202"]) == {"101": None, "202": 8}
    with store.store.connect() as db:
        db.execute("UPDATE telegram_poll_batches SET expires=0 WHERE id=?", (batch,))
    with pytest.raises(ProviderError):
        store.acknowledge("alice", batch)


@pytest.mark.parametrize(
    "args",
    [
        {"chat_ids": []},
        {"chat_ids": ["101", "101"]},
        {"chat_ids": ["@person"]},
        {"chat_ids": ["101"], "max_per_chat": 51},
    ],
)
async def test_poll_bad_arguments_before_provider(config, args):
    adapter = PollAdapter()
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_poll_messages", args)
    assert not adapter.calls


async def test_poll_partial_failure_does_not_advance_or_prepare_batch(config):
    adapter = PollAdapter()
    adapter.failure = ProviderError("fixture failure")
    async with Client(create_server(config, transport="stdio", adapter=adapter)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})
    with StateStore(config).connect() as db:
        assert db.execute("SELECT count(*) FROM telegram_poll_batches").fetchone()[0] == 0


async def test_pinned_client_poll_min_id_order_and_no_global_event_buffer():
    calls = []

    async def get_messages(entity, **kwargs):
        calls.append((entity, kwargs))
        return [SimpleNamespace(id=12, message="hello", out=False, media=True)]

    async def resolve(identifier, client):
        return identifier

    async def load():
        pass

    runtime = SimpleNamespace(
        get_client=lambda account: SimpleNamespace(get_messages=get_messages), resolve_entity=resolve
    )
    adapter = SimpleNamespace(_runtime=runtime, _embedded_server=load)
    result = await read_since(adapter, "101", 10, 20, "default")
    assert calls == [(101, {"min_id": 10, "reverse": True, "limit": 21})]
    assert result[0]["has_media"] and result[0]["message_id"] == 12


async def test_resolved_fallback_chat_is_rejected_before_messages():
    async def wrong(identifier, client):
        return -101

    async def load():
        pass

    adapter = SimpleNamespace(
        _runtime=SimpleNamespace(get_client=lambda label: object(), resolve_entity=wrong),
        _embedded_server=load,
    )
    with pytest.raises(ProviderError, match="differs"):
        await read_since(adapter, "101", 10, 20, "default")


async def test_poll_http_scopes_and_batch_owner(config):
    from fastmcp.server.auth.providers.jwt import RSAKeyPair
    from test_server import connection

    pair = RSAKeyPair.generate()
    settings = replace(config, public_key=pair.public_key)
    adapter = PollAdapter()
    server = create_server(settings, adapter=adapter)

    def token(actor="alice", scope="telegram:read", account="default"):
        return pair.create_token(
            subject=actor,
            issuer=config.issuer,
            audience=config.audience,
            scopes=[scope],
            expires_in_seconds=60,
            additional_claims={"account_id": account},
        )

    for bearer in [token(scope="telegram:write"), token(account="foreign")]:
        async with connection(server, bearer) as client:
            with pytest.raises(ToolError):
                await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})
    assert not adapter.calls
    async with connection(server, token()) as client:
        batch = (await client.call_tool("telegram_poll_messages", {"chat_ids": ["101"]})).data["batch_id"]
    async with connection(server, token(actor="bob")) as client:
        with pytest.raises(ToolError):
            await client.call_tool("telegram_acknowledge_poll", {"batch_id": batch})
    assert len(adapter.calls) == 1 and adapter.calls[0][3] == "default"

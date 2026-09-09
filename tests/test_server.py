from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.resources import files

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from test_runtime import SEND, FixtureAdapter

from zai_telegram.server import create_server


@asynccontextmanager
async def connection(server, token):
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)

    def factory(**kwargs):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), **kwargs)

    async with (
        app.router.lifespan_context(app),
        Client(
            StreamableHttpTransport("http://fixture/mcp", auth=token, httpx_client_factory=factory)
        ) as client,
    ):
        yield client


async def test_frozen_original_contract_exactly_matches_standalone(config):
    expected = json.loads(files("zai_telegram").joinpath("_contracts/telegram.json").read_text())["tools"]
    async with Client(create_server(config, transport="stdio", adapter=FixtureAdapter())) as client:
        actual = {
            tool.name: {
                "inputSchema": tool.input_schema,
                "outputSchema": tool.output_schema,
                "description": tool.description,
            }
            for tool in await client.list_tools()
        }
    assert {name: actual[name] for name in expected} == expected
    assert set(actual) == set(expected) | {"telegram_poll_messages", "telegram_acknowledge_poll"}


async def test_authenticated_http_scopes_bindings_and_outbox_ownership(config):
    pair = RSAKeyPair.generate()
    settings = replace(config, public_key=pair.public_key)
    adapter = FixtureAdapter()
    server = create_server(settings, adapter=adapter)

    def token(actor="alice", scopes=("telegram:read", "telegram:write"), account="default", **claims):
        return pair.create_token(
            subject=actor,
            issuer=settings.issuer,
            audience=settings.audience,
            scopes=list(scopes),
            expires_in_seconds=60,
            additional_claims={"account_id": account, **claims},
        )

    async with connection(server, token(scopes=["telegram:read"])) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert len(names) == 9 and "telegram_send_message" not in names
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", SEND)
    for bearer in [token(account="other"), token(actor="unbound")]:
        async with connection(server, bearer) as client:
            with pytest.raises(ToolError):
                await client.call_tool("telegram_list_chats", {})
    async with connection(server, token(telegram_account="second", telegram_rate_limit=9999)) as client:
        await client.call_tool("telegram_send_message", SEND)
        assert (await client.call_tool("telegram_outbox_status", {})).data["records"]
    async with connection(server, token(actor="bob")) as client:
        assert (await client.call_tool("telegram_outbox_status", {})).data["records"] == []
    assert adapter.calls[0][1]["account"] == "default"
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code in {401, 403}


async def test_real_stdio_discovers_without_loading_telegram_sessions(config, tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps(dict(config.bindings)), encoding="utf-8")
    bindings.chmod(0o600)
    env = {
        "TELEGRAM_STATE_PATH": str(tmp_path / "stdio.sqlite"),
        "TELEGRAM_SECRET_FILE": str(config.secret_path),
        "TELEGRAM_BINDINGS_FILE": str(bindings),
    }
    async with Client(StdioTransport(command=sys.executable, args=["-m", "zai_telegram"], env=env)) as client:
        assert len(await client.list_tools()) == 9


def test_http_requires_explicit_verification_key(config):
    with pytest.raises(ValueError, match="public"):
        create_server(config)


async def test_stdio_scope_restriction_is_enforced_even_when_write_mode_is_enabled(config):
    adapter = FixtureAdapter()
    settings = replace(config, local_scopes=frozenset({"telegram:read"}))
    async with Client(create_server(settings, transport="stdio", adapter=adapter)) as client:
        assert len(await client.list_tools()) == 9
        with pytest.raises(ToolError):
            await client.call_tool("telegram_send_message", SEND)
    assert adapter.calls == []

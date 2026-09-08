"""Discover the committed Telegram baseline with synthetic gateway auth only."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp_platform.app import create_platform
from mcp_platform.auth.service import TokenService
from mcp_platform.providers.registry import ProviderRegistry
from mcp_platform.settings import Settings
from mcp_platform.store import InMemoryStore


async def main():
    output = Path(sys.argv[1])
    assert not output.exists()
    settings = Settings(environment="test", public_base_url="http://fixture")
    store = InMemoryStore()
    principal = await store.create_principal("contract-freeze")
    issued = await TokenService(store, settings.token_pepper()).issue(
        principal, "contract-freeze", {"mcp:connect", "telegram:read", "telegram:write"}
    )
    server = create_platform(settings, store, ProviderRegistry(settings, store))
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)

    def factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fixture", **kwargs)

    transport = StreamableHttpTransport("http://fixture/mcp", auth=issued.plaintext, httpx_client_factory=factory)
    async with app.router.lifespan_context(app), Client(transport) as client:
        tools = {tool.name: {"inputSchema": tool.input_schema, "outputSchema": tool.output_schema,
                            "description": tool.description}
                 for tool in await client.list_tools() if tool.name.startswith("telegram_")}
    assert len(tools) == 12, list(tools)
    receipt = {"gateway_revision": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
               "method": "authenticated discovery only; no provider data API", "tools": tools}
    output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Froze {len(tools)} original Telegram schemas/descriptions")


asyncio.run(main())

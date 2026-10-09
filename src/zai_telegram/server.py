from __future__ import annotations

import argparse
import inspect
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier

from zai_telegram import __version__
from zai_telegram.config import ServiceConfig
from zai_telegram.onboarding import check_config, load_config
from zai_telegram.polling import register_polling
from zai_telegram.runtime import Runtime
from zai_telegram.tools import register_tools
from zai_telegram.update_check import cached_update_hint, check_for_update, refresh_in_background


class ToolRegistrar:
    def __init__(self, server: FastMCP, runtime: Runtime):
        self.server, self.runtime = server, runtime

    def tool(self, **options: Any) -> Callable[..., Any]:
        def decorate(function: Callable[..., Any]) -> Any:
            # FastMCP skips token authorization for unauthenticated stdio.
            # Its scope surface must therefore be selected explicitly here.
            if self.runtime.transport == "stdio":
                try:
                    self.runtime.authorize(function.__name__)
                except PermissionError:
                    return function
            signature = inspect.signature(function)

            @wraps(function)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                return await self.runtime.execute(
                    function.__name__, dict(bound.arguments), lambda: function(*args, **kwargs)
                )

            return self.server.tool(**options)(wrapped)

        return decorate


def create_server(
    config: ServiceConfig, *, transport: str = "http", adapter: Callable[..., Any] | None = None
) -> FastMCP:
    if transport not in {"stdio", "http"}:
        raise ValueError("unsupported transport")
    if transport == "http" and not config.public_key:
        raise ValueError("HTTP requires an RSA public verification key")
    auth = (
        JWTVerifier(
            public_key=config.public_key, algorithm="RS256", issuer=config.issuer, audience=config.audience
        )
        if transport == "http"
        else None
    )
    runtime = Runtime(config, transport, adapter)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield
        finally:
            close = getattr(runtime.adapter, "close", None)
            if close:
                await close()

    server = FastMCP(
        "Telegram MCP",
        # Cache-only: startup never waits on the network for this hint.
        instructions=cached_update_hint(),
        version=__version__,
        auth=auth,
        mask_error_details=True,
        lifespan=lifespan,
    )
    registrar = ToolRegistrar(server, runtime)
    register_tools(registrar, runtime)
    register_polling(registrar, runtime)
    register_update_check(server, runtime)
    return server


def register_update_check(server: FastMCP, runtime: Runtime) -> None:
    # Registered directly: it needs no Telegram account binding or provider budget.
    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    )
    def telegram_check_update(force: bool = False) -> dict[str, Any]:
        """Check GitHub for a newer zai-telegram-mcp release and suggest the update command.

        Read-only and suggest-only: nothing is downloaded or installed. Uses a 24h cache
        unless force=true; disabled by TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1.
        """
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        return check_for_update(force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Telegram MCP")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8816)
    parser.add_argument("--config", help="Operator JSON settings; paths relative to this file")
    parser.add_argument(
        "--check-config", action="store_true", help="Check local settings without provider calls"
    )
    args = parser.parse_args()
    try:
        load_config(args.config)
        config = ServiceConfig.from_env()
        if args.check_config:
            result = check_config(config, args.transport)
            print(json.dumps(result, sort_keys=True))
            parser.exit(0 if result["ready"] else 2)
        server = create_server(config, transport=args.transport)
        refresh_in_background()
    except (ValueError, OSError) as exc:
        parser.exit(2, f"configuration error: {type(exc).__name__}; check credential and policy files\n")
    if args.transport == "http":
        server.run(
            transport="http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
            show_banner=False,
            log_level="warning",
        )
    else:
        server.run(transport="stdio", show_banner=False, log_level="warning")

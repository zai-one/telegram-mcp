from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier

from zai_telegram import __version__
from zai_telegram.agent_layer import TOOL_DESCRIPTIONS, TOOL_TITLES, build_instructions, register_agent_layer
from zai_telegram.config import ServiceConfig
from zai_telegram.errors import policy_error
from zai_telegram.onboarding import check_config, load_config
from zai_telegram.polling import register_polling
from zai_telegram.runtime import Runtime
from zai_telegram.tools import register_tools
from zai_telegram.toolsets import (
    LISTING_TOOLS,
    Surface,
    build_surface,
    chat_targets,
    filter_result,
    surface_from_env,
    write_denied_by_allowlist,
)
from zai_telegram.transport import LocalPreDispatchDenied, ProviderAdmissionDenied
from zai_telegram.update_check import cached_update_hint, check_for_update, refresh_in_background


class ToolRegistrar:
    def __init__(self, server: FastMCP, runtime: Runtime, surface: Surface | None = None):
        self.server, self.runtime = server, runtime
        self.surface = surface or build_surface()

    def _admit(self, name: str, arguments: dict[str, Any]) -> None:
        """Toolset write-category and per-chat ACL checks, before any provider call."""
        if name == "telegram_write" and arguments.get("tool") not in self.surface.write_tools:
            raise policy_error("tool_disabled")
        chats = self.surface.chats
        if name == "telegram_write" and write_denied_by_allowlist(chats, arguments):
            # Account-wide or untargeted writes would bypass the allowlist.
            raise policy_error("chat_access_denied")
        if chats.active and any(not chats.permits(target) for target in chat_targets(name, arguments)):
            raise policy_error("chat_access_denied")

    def tool(self, **options: Any) -> Callable[..., Any]:
        def decorate(function: Callable[..., Any]) -> Any:
            if not self.surface.exposes(function.__name__):
                return function
            name = function.__name__
            opts = dict(options)
            description = TOOL_DESCRIPTIONS.get(name)
            if description and "description" not in opts:
                opts["description"] = description
            if name in TOOL_TITLES:
                opts["title"] = TOOL_TITLES[name]
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
                arguments = dict(bound.arguments)

                async def guarded() -> Any:
                    self._admit(name, arguments)
                    result = await function(*args, **kwargs)
                    if name in LISTING_TOOLS:
                        result = filter_result(result, self.surface.chats)
                    return result

                return await self.runtime.execute(name, arguments, guarded)

            return self.server.tool(**opts)(wrapped)

        return decorate


def create_server(
    config: ServiceConfig,
    *,
    transport: str = "http",
    adapter: Callable[..., Any] | None = None,
    surface: Surface | None = None,
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
    surface = surface or surface_from_env()

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
        # Router (static) + cache-only update hint: startup never waits on the network.
        instructions=build_instructions(surface, cached_update_hint()),
        version=__version__,
        auth=auth,
        mask_error_details=True,
        lifespan=lifespan,
    )
    registrar = ToolRegistrar(server, runtime, surface)
    register_tools(registrar, runtime)
    register_polling(registrar, runtime)
    register_update_check(registrar, runtime)
    register_agent_layer(server, surface)
    return server


def register_update_check(server: Any, runtime: Runtime) -> None:
    """Register the local update check through the same registrar as the Telegram tools.

    The registrar supplies the stdio scope filter, the authenticated identity and
    account binding, the size bound and the audit row. The per-account admission
    (quota, concurrency lease, cooldown) is taken explicitly because the check
    makes no Telegram call that would otherwise pass through the provider registry.
    """

    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    )
    async def telegram_check_update(force: bool = False) -> dict[str, Any]:
        """Check GitHub for a newer zai-telegram-mcp release and suggest the update command.

        Read-only and suggest-only: nothing is downloaded or installed. Uses a 24h cache
        unless force=true; disabled by TELEGRAM_MCP_DISABLE_UPDATE_CHECK=1.
        """
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        access = runtime.current_access()
        try:
            runtime.store.admit(access.execution_id, access.principal_id, write=False)
        except ProviderAdmissionDenied as exc:
            raise LocalPreDispatchDenied(str(exc), retry_after_seconds=exc.retry_after_seconds) from None
        # The GitHub request blocks for up to its timeout; keep it off the event loop.
        return await asyncio.to_thread(check_for_update, force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Telegram MCP")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8816)
    parser.add_argument("--config", help="Operator JSON settings; paths relative to this file")
    parser.add_argument("--toolsets", help="Toolsets: comma list, 'default' or 'all' (TELEGRAM_TOOLSETS)")
    parser.add_argument("--read-only", action="store_true", help="Hide every write tool (TELEGRAM_READ_ONLY)")
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
        surface = surface_from_env(cli_toolsets=args.toolsets, cli_read_only=args.read_only)
        server = create_server(config, transport=args.transport, surface=surface)
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

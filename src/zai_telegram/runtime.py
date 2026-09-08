from __future__ import annotations

import asyncio
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastmcp.server.dependencies import get_access_token

from zai_telegram.adapter import TelegramAdapter
from zai_telegram.config import ServiceConfig
from zai_telegram.errors import SafeToolError, safe_provider_error
from zai_telegram.sanitizer import DURABLE_SANITIZER_LIMITS, sanitize_provider_response
from zai_telegram.state import StateStore
from zai_telegram.transport import (
    LocalPreDispatchDenied,
    ProviderAdmissionDenied,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransportError,
    canonical_json,
    request_hash,
)

OPERATION_TIMEOUT_SECONDS = 125

WRITE_TOOLS = frozenset(
    {
        "telegram_send_message",
        "telegram_reply_to_message",
        "telegram_send_many",
        "telegram_outbox_status",
        "telegram_write",
    }
)


@dataclass(frozen=True)
class CallState:
    execution_id: str
    principal_id: str


class Registry:
    def __init__(self, runtime):
        self.runtime = runtime

    def telegram(self):
        return self.runtime.adapter

    async def ensure_enabled(self, provider):
        if provider != "telegram" or not self.runtime.config.enabled:
            raise PermissionError("provider is disabled")

    async def _invoke(self, actor, provider, operation, factory, *, write):
        state = self.runtime.state.get()
        if str(actor) != state.principal_id:
            raise PermissionError("execution identity mismatch")
        await self.ensure_enabled(provider)
        label = self.runtime.config.bindings.get(state.principal_id)
        if not self.runtime.adapter.has_account(label):
            raise PermissionError("bound Telegram account is not configured")
        try:
            self.runtime.store.admit(state.execution_id, state.principal_id, write=write)
        except ProviderAdmissionDenied as exc:
            # Only this pre-factory location may certify that nothing was sent.
            raise LocalPreDispatchDenied(str(exc), retry_after_seconds=exc.retry_after_seconds) from None
        try:
            return self.runtime.clean(await factory())
        except ProviderRateLimited as exc:
            if not isinstance(exc, ProviderAdmissionDenied):
                self.runtime.store.cooldown(state.principal_id, exc.retry_after_seconds or 60)
            raise
        except (ProviderTimeoutError, ProviderTransportError):
            self.runtime.store.cooldown(state.principal_id, 60)
            raise

    async def call(self, actor, provider, operation, factory):
        return await self._invoke(actor, provider, operation, factory, write=True)

    async def read(self, actor, provider, operation, arguments, factory, *, validate=None):
        if validate:
            validate()
        return await self._invoke(actor, provider, operation, factory, write=False)


class Runtime:
    def __init__(self, config: ServiceConfig, transport: str, adapter=None):
        self.config = self.settings = config
        self.transport = transport
        self.store = StateStore(config)
        self.adapter = adapter or TelegramAdapter(
            config.secret_path, write_enabled=config.telegram_write_enabled, strict_secret=True
        )
        self.state: ContextVar[CallState] = ContextVar("telegram_call")
        self.registry = Registry(self)
        # One immutable credential set per process; never consult caller-provided paths.
        self.secret_values = config.secrets

    def require_scopes(self, *scopes):
        def check(context):
            available = (
                self.config.local_scopes
                if self.transport == "stdio"
                else context.token.scopes
                if context.token
                else []
            )
            return set(scopes) <= set(available)

        return check

    def identity(self):
        if self.transport == "stdio":
            actor = self.config.principal_id
        else:
            token = get_access_token()
            claims = token.claims if token else {}
            actor, expires = claims.get("sub"), claims.get("exp")
            if (
                not isinstance(actor, str)
                or not 1 <= len(actor) <= 256
                or claims.get("account_id") != self.config.account_id
                or not isinstance(expires, (int, float))
                or isinstance(expires, bool)
                or not math.isfinite(expires)
                or expires <= time.time()
            ):
                raise PermissionError("account-bound authenticated identity required")
        if actor not in self.config.bindings:
            raise PermissionError("server-owned Telegram account binding required")
        return actor

    def current_access(self):
        return self.state.get()

    def authorize(self, tool):
        required = "telegram:write" if tool in WRITE_TOOLS else "telegram:read"
        token = get_access_token() if self.transport == "http" else None
        available = self.config.local_scopes if self.transport == "stdio" else token.scopes if token else []
        if required not in available:
            raise PermissionError("required Telegram scope missing")

    def clean(self, value):
        def literal(item):
            if isinstance(item, str):
                for credential in self.secret_values:
                    if credential:
                        item = item.replace(credential, "***redacted***")
                return item
            if isinstance(item, list):
                return [literal(child) for child in item]
            if isinstance(item, dict):
                return {literal(key): literal(child) for key, child in item.items()}
            return item

        return literal(sanitize_provider_response(value, limits=DURABLE_SANITIZER_LIMITS))

    async def execute(self, tool: str, arguments: dict[str, Any], factory):
        execution_id, token, outcome = uuid4().hex, None, "error"
        began = False
        try:
            self.authorize(tool)
            actor = self.identity()
            if len(canonical_json(arguments).encode()) > 1_048_576:
                raise ValueError("request exceeds bounded input size")
            self.store.begin(execution_id, actor, tool, request_hash({"tool": tool, "arguments": arguments}))
            began = True
            token = self.state.set(CallState(execution_id, actor))
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                result = await factory()
            outcome = "success"
            return self.clean(result)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except SafeToolError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                exc = ProviderTimeoutError("operation deadline exceeded")
            raise safe_provider_error("telegram", exc) from None
        finally:
            if token is not None:
                self.state.reset(token)
            if began:
                self.store.finish(execution_id, outcome)

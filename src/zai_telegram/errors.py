from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import ToolError

from zai_telegram.transport import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderHttpError,
    ProviderNotFoundError,
    ProviderRateLimited,
    ProviderRequestError,
    ProviderRequestTooLarge,
    ProviderResponseError,
    ProviderResponseTooLarge,
    ProviderTimeoutError,
    ProviderTransientHttpError,
    ProviderTransportError,
)

# Short, content-free guidance per error code: what the agent should do next.
ERROR_HINTS: dict[str, str] = {
    "provider_rate_limited": "Telegram FLOOD_WAIT or local rate limit; wait retry_after_seconds, then retry once.",
    "provider_authentication_failed": "Session invalid or expired; ask the user to re-run zai-telegram-login.",
    "provider_resource_not_found": "Unknown chat/message; resolve IDs with telegram_list_chats or telegram_resolve_recipient.",
    "provider_payload_too_large": "Narrow the request: lower limit/page_size or split the message.",
    "provider_request_invalid": "Arguments rejected; fix them per the tool schema, do not retry unchanged.",
    "provider_timeout": "Telegram did not answer in time; retry once with the same idempotency_key.",
    "provider_transport_failed": "Network problem; retry later with the same idempotency_key.",
    "provider_temporarily_unavailable": "Telegram is temporarily unavailable; retry later.",
    "provider_response_invalid": "Unexpected upstream response; do not retry blindly, report to the user.",
    "provider_http_error": "Upstream error; report to the user.",
    "validation_failed": "Arguments failed validation; fix them per the tool schema, do not retry unchanged.",
    "provider_disabled": "Telegram, writes or this account are disabled by the operator; tell the user.",
    "operation_failed": "Unexpected failure; report to the user instead of retrying.",
    "chat_access_denied": "Chat is outside TELEGRAM_ALLOWED_CHATS/DENIED_CHATS; ask the user, never work around it.",
    "tool_disabled": "Tool or write category is not enabled; see telegram://toolsets for the env to enable it.",
}


class SafeToolError(ToolError):
    def __init__(self, envelope: dict[str, Any]) -> None:
        code = str(envelope["code"])
        envelope = {
            **envelope,
            "hint": envelope.get("hint") or ERROR_HINTS.get(code, ERROR_HINTS["operation_failed"]),
            "docs": f"telegram://guide/errors#{code}",
        }
        self.envelope = envelope
        self.error_code = str(envelope["code"])
        super().__init__(json.dumps(envelope, sort_keys=True, separators=(",", ":")))


def safe_provider_error(provider: str, exc: Exception) -> SafeToolError:
    code = "provider_error"
    retryable = False
    retry_after: int | None = None
    if isinstance(exc, ProviderRateLimited):
        code = "provider_rate_limited"
        retryable = True
        retry_after = exc.retry_after_seconds
    elif isinstance(exc, ProviderAuthenticationError):
        code = "provider_authentication_failed"
    elif isinstance(exc, ProviderNotFoundError):
        code = "provider_resource_not_found"
    elif isinstance(exc, (ProviderRequestTooLarge, ProviderResponseTooLarge)):
        code = "provider_payload_too_large"
    elif isinstance(exc, ProviderRequestError):
        code = "provider_request_invalid"
    elif isinstance(exc, ProviderTimeoutError):
        code = "provider_timeout"
        retryable = True
    elif isinstance(exc, ProviderTransportError):
        code = "provider_transport_failed"
        retryable = True
    elif isinstance(exc, ProviderTransientHttpError):
        code = "provider_temporarily_unavailable"
        retryable = True
    elif isinstance(exc, ProviderResponseError):
        code = "provider_response_invalid"
    elif isinstance(exc, ProviderHttpError):
        code = "provider_http_error"
    elif isinstance(exc, ValueError):
        code = "validation_failed"
    elif isinstance(exc, PermissionError):
        code = "provider_disabled"
    elif not isinstance(exc, ProviderError):
        code = "operation_failed"
    envelope: dict[str, Any] = {
        "code": code,
        "provider": provider,
        "retryable": retryable,
    }
    if retry_after is not None:
        envelope["retry_after_seconds"] = retry_after
    return SafeToolError(envelope)


def policy_error(code: str, *, retryable: bool = False) -> SafeToolError:
    """Local policy refusal (ACL, toolsets) in the same envelope as provider errors."""
    return SafeToolError({"code": code, "provider": "telegram", "retryable": retryable})

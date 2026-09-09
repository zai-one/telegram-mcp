from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import json
import os
import re
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from zai_telegram.media_policy import FILE_FIELDS, MediaPolicy, download, exact_chat, verify_receipt
from zai_telegram.secrets import read_private_secret_env, read_secret_env
from zai_telegram.transport import ProviderError, ProviderTimeoutError, ProviderTransportError
from zai_telegram.vendor_integrity import assert_pinned_vendor

TELEGRAM_TOOL_MAP = {
    "telegram_list_chats": "list_chats",
    "telegram_search_messages": "search_messages",
    "telegram_get_messages": "get_messages",
    "telegram_get_chat": "get_chat",
    # Correspondence-v1 read primitives. Some are intentionally internal to
    # composite platform tools, but remain on the same reviewed read allowlist.
    "telegram_get_inbox": "list_chats",
    "telegram_get_history": "get_history",
    "telegram_search_contacts": "search_contacts",
    "telegram_search_public_chats": "search_public_chats",
}

# Frozen write allowlist: upstream tool name -> risk tier. Derived once from the
# vendored telegram-mcp v3.2.0 annotations (readOnlyHint is not True) and pinned
# here so a vendor upgrade can never auto-expose a new write tool — a new
# upstream tool stays inert until it is added to this map on review. Tiers drive
# rate-limit and audit strictness, not human approval (autonomous by decision).
TELEGRAM_WRITE_ALLOWLIST: dict[str, str] = {
    # messages
    "send_message": "medium",
    "send_scheduled_message": "medium",
    "delete_scheduled_message": "medium",
    "press_inline_button": "high",
    "forward_message": "medium",
    "forward_messages": "medium",
    "edit_message": "high",
    "delete_message": "high",
    "delete_chat_history": "high",
    "delete_messages_bulk": "high",
    "pin_message": "medium",
    "unpin_message": "medium",
    "unpin_all_messages": "medium",
    "mark_as_read": "low",
    "reply_to_message": "medium",
    "create_poll": "medium",
    "send_reaction": "medium",
    "remove_reaction": "medium",
    "save_draft": "low",
    "clear_draft": "low",
    # chats
    "subscribe_public_channel": "high",
    "enable_forum_topics": "high",
    "create_forum_topic": "medium",
    "mute_chat": "low",
    "unmute_chat": "low",
    "archive_chat": "low",
    "unarchive_chat": "low",
    # contacts
    "add_contact": "low",
    "delete_contact": "low",
    "block_user": "high",
    "unblock_user": "high",
    "import_contacts": "medium",
    "send_contact": "medium",
    # groups
    "create_group": "medium",
    "invite_to_group": "high",
    "leave_chat": "high",
    "create_channel": "medium",
    "edit_chat_title": "medium",
    "edit_chat_photo": "high",
    "edit_chat_about": "medium",
    "delete_chat_photo": "medium",
    "promote_admin": "high",
    "demote_admin": "high",
    "ban_user": "high",
    "unban_user": "high",
    "set_default_chat_permissions": "high",
    "toggle_slow_mode": "high",
    "edit_admin_rights": "high",
    "join_chat_by_link": "high",
    "import_chat_invite": "high",
    # media
    "send_file": "high",
    "send_album": "high",
    "download_media": "low",
    "send_voice": "high",
    "upload_file": "medium",
    "send_sticker": "high",
    "send_gif": "medium",
    # profile
    "update_profile": "high",
    "set_profile_photo": "high",
    "delete_profile_photo": "high",
    "set_privacy_settings": "high",
    "set_bot_commands": "medium",
    # folders
    "create_folder": "low",
    "add_chat_to_folder": "low",
    "remove_chat_from_folder": "low",
    "delete_folder": "low",
    "reorder_folders": "low",
}

# Typed convenience write tools exposed on the platform surface -> upstream.
TELEGRAM_WRITE_TOOL_MAP = {
    "telegram_send_message": "send_message",
    "telegram_reply_to_message": "reply_to_message",
}

_REQUIRED_UPSTREAM_TOOLS = set(TELEGRAM_TOOL_MAP.values()) | {"search_global"}
_HIGH_RISK_WRITE_TOOLS = frozenset(name for name, tier in TELEGRAM_WRITE_ALLOWLIST.items() if tier == "high")


def _json_safe_result(value: Any) -> Any:
    """Coerce an upstream tool result into a JSON-serializable value.

    The embedded FastMCP client returns structured-output objects for some
    tools; these must be flattened before they are stored in the durable
    idempotency ledger or returned to the caller. Two shapes occur: Pydantic
    models, and the dynamic dataclasses FastMCP builds from a tool's output
    schema (``fastmcp.utilities.json_schema_type.Root``). The dataclass shape
    has no ``model_dump``, so without an explicit branch it falls all the way
    through to ``str(value)`` and a structured result is persisted as an
    unparseable Python repr.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # pragma: no cover - defensive
            return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _json_safe_result(dataclasses.asdict(value))
        except Exception:  # pragma: no cover - defensive
            return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe_result(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe_result(item) for item in value]
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    return str(value)


def _tool_error_detail(kind: str, exc: Exception) -> str:
    """Carry the upstream reason inward without widening the client envelope.

    ``safe_provider_error`` builds the client-facing envelope from the error
    class alone, so this text never reaches an MCP caller. It only reaches the
    server log and the ``__cause__`` chain - which is exactly what was missing
    when a vendor argument mismatch surfaced as an opaque ``provider_error``
    with no traceback at all.
    """
    detail = " ".join(str(exc).split())[:300]
    base = f"embedded Telegram {kind} returned an error"
    return f"{base}: {detail}" if detail else base


def _fastmcp_result_value(result: Any) -> Any:
    """Flatten FastMCP result wrappers consistently for reads and writes.

    FastMCP 3 wraps tools annotated as returning a scalar in a dynamic
    ``{"result": ...}`` output model.  The Telegram vendor tools return JSON
    envelopes as strings, so leaking that wrapper to the platform turns a
    valid ``{"results": [...]}`` payload into an unexpected outer mapping.
    """
    texts = [text for item in result.content if isinstance(text := getattr(item, "text", None), str)]
    structured = _json_safe_result(result.data) if result.data is not None else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        root = structured["result"]
        if isinstance(root, dict | list):
            value: Any = root
        elif texts:
            value = texts[0] if len(texts) == 1 else texts
        else:
            value = root
    elif isinstance(structured, dict | list):
        value = structured
    elif texts:
        value = texts[0] if len(texts) == 1 else texts
    else:
        value = result.content
    return _json_safe_result(value)


_FIXED_SECRET_KEYS = {
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_CONNECTION",
    "TELEGRAM_DEVICE_MODEL",
    "TELEGRAM_SYSTEM_VERSION",
    "TELEGRAM_APP_VERSION",
    "TELEGRAM_LANG_CODE",
    "TELEGRAM_SYSTEM_LANG_CODE",
}
_LABELLED_SECRET_KEY = re.compile(
    r"TELEGRAM_(?:SESSION_STRING|CONNECTION|DEVICE_MODEL|SYSTEM_VERSION|APP_VERSION|LANG_CODE|SYSTEM_LANG_CODE)_[A-Z0-9_]+"
)
_ACCOUNT_LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")

# Telegram can legitimately take longer than the historical 30-second MCP
# transport default while its MTProto sender reconnects.  Keep this bounded,
# but above the slowest live readback observed during the SDK 2 migration.
TELEGRAM_UPSTREAM_TIMEOUT_SECONDS = 120

_PROCESS_RUNTIME_LOCK = threading.RLock()
_PROCESS_FINGERPRINT: str | None = None


class TelegramAdapter:
    """Run the pinned Telegram MCP runtime inside the platform process."""

    def __init__(
        self,
        secret_path: Path,
        *,
        write_enabled: bool = False,
        strict_secret: bool = False,
        media_policy: MediaPolicy | None = None,
    ) -> None:
        self.secret_path = secret_path
        self.write_enabled = write_enabled
        self.strict_secret = strict_secret
        self.media_policy = media_policy or MediaPolicy()
        self._server: Any | None = None
        self._runtime: ModuleType | None = None
        self._load_lock = asyncio.Lock()

    def _secret(self) -> dict[str, str]:
        reader = read_private_secret_env if self.strict_secret else read_secret_env
        return reader(self.secret_path)

    @staticmethod
    def _allowed_secret_key(key: str) -> bool:
        return key in _FIXED_SECRET_KEYS or _LABELLED_SECRET_KEY.fullmatch(key) is not None

    def configured_account_labels(self) -> frozenset[str]:
        secret = self._secret()
        labels: set[str] = set()
        if secret.get("TELEGRAM_SESSION_STRING"):
            labels.add("default")
        prefix = "TELEGRAM_SESSION_STRING_"
        for key, value in secret.items():
            if value and key.startswith(prefix):
                label = key[len(prefix) :].lower()
                if _ACCOUNT_LABEL.fullmatch(label) is not None:
                    labels.add(label)
        return frozenset(labels)

    def has_account(self, label: str) -> bool:
        return label in self.configured_account_labels()

    async def _embedded_server(self) -> Any:
        global _PROCESS_FINGERPRINT
        # Preserve explicit in-process server injection used by offline callers.
        # Real loaded runtimes always set both fields and still check credentials.
        if self._server is not None and self._runtime is None:
            return self._server
        async with self._load_lock:
            secret = {key: value for key, value in self._secret().items() if self._allowed_secret_key(key)}
            required = {"TELEGRAM_API_ID", "TELEGRAM_API_HASH"}
            missing = sorted(required - secret.keys())
            has_session = any(
                key == "TELEGRAM_SESSION_STRING" or key.startswith("TELEGRAM_SESSION_STRING_")
                for key in secret
                if secret[key]
            )
            if missing or not has_session:
                detail = ", ".join(missing + ([] if has_session else ["TELEGRAM_SESSION_STRING"]))
                raise ProviderError(f"embedded Telegram runtime secret is incomplete: {detail}")
            # Read-only keeps the upstream server as a second, independent gate.
            # When write is enabled we start from "all" and then prune to the
            # frozen allowlist, so a vendor upgrade can never auto-expose a new
            # write tool — the platform surface stays deterministic either way.
            mode = "all" if self.write_enabled else "read-only"
            fingerprint = hashlib.sha256(
                json.dumps([secret, mode, self.media_policy.fingerprint()], sort_keys=True).encode()
            ).hexdigest()
            with _PROCESS_RUNTIME_LOCK:
                if _PROCESS_FINGERPRINT is not None and fingerprint != _PROCESS_FINGERPRINT:
                    raise ProviderError("Telegram process configuration changed; restart required")
                if self._server is not None:
                    return self._server
                # Claim before import: failed/partial imports also require restart
                # before a different credential set can be used in this process.
                _PROCESS_FINGERPRINT = fingerprint
                for key in list(os.environ):
                    if key.startswith("TELEGRAM_"):
                        del os.environ[key]
                os.environ.update(secret)
                os.environ["TELEGRAM_EXPOSED_TOOLS"] = mode
                os.environ["PYTHON_DOTENV_DISABLED"] = "1"
                try:
                    assert_pinned_vendor()
                    runtime = importlib.import_module("zai_telegram._vendor.telegram_mcp.runtime")
                    importlib.import_module("zai_telegram._vendor.telegram_mcp.tools")
                except (ImportError, RuntimeError, SystemExit, TypeError, ValueError) as exc:
                    raise ProviderError("pinned embedded Telegram runtime failed to load") from exc
                runtime._apply_exposed_tools_mode(runtime.mcp, mode)
                runtime.SERVER_ALLOWED_ROOTS = list(self.media_policy.roots)
                if self.write_enabled:
                    self._prune_to_write_allowlist(runtime.mcp)
                self._runtime = runtime
                self._server = runtime.mcp
                return self._server

    @staticmethod
    def _prune_to_write_allowlist(server: Any) -> None:
        """Remove any exposed write tool that is not in the frozen allowlist.

        Read tools (readOnlyHint True) are kept; every write tool must be an
        explicit, reviewed entry in TELEGRAM_WRITE_ALLOWLIST. This is the drift
        guard: a new upstream write tool is inert until reviewed and pinned.
        """
        # FastMCP 4 moved mutable component storage from the removed
        # ``_tool_manager`` compatibility shim to the local provider.
        manager = server._local_provider
        tools = [component for component in manager._components.values() if isinstance(component, Tool)]
        for tool in tools:
            annotations = getattr(tool, "annotations", None)
            read_only = bool(getattr(annotations, "read_only_hint", False))
            if read_only:
                continue
            if tool.name not in TELEGRAM_WRITE_ALLOWLIST:
                manager.remove_tool(tool.name)

    async def status(self) -> dict[str, Any]:
        server = await self._embedded_server()
        async with Client(FastMCPTransport(server), timeout=10) as client:
            tools = await client.list_tools()
        names = {tool.name for tool in tools}
        missing = sorted(_REQUIRED_UPSTREAM_TOOLS - names)
        exposed_writes = sorted(names & set(TELEGRAM_WRITE_ALLOWLIST)) if self.write_enabled else []
        return {
            "ready": not missing,
            "protocol": "in-memory-mcp",
            "deployment": "embedded-fastmcp-runtime",
            "required_tools_present": not missing,
            "missing_tools": missing,
            "write_tools_exposed_by_platform": self.write_enabled,
            "write_allowlist_count": len(TELEGRAM_WRITE_ALLOWLIST),
            "write_tools_exposed": exposed_writes,
            "configured_accounts": len(self.configured_account_labels()),
        }

    @staticmethod
    def risk_tier(upstream_tool: str) -> str:
        return TELEGRAM_WRITE_ALLOWLIST.get(upstream_tool, "unknown")

    @staticmethod
    def _runtime_error(exc: BaseException) -> ProviderError:
        """Map an embedded-runtime failure onto the platform error taxonomy.

        Telethon reaches the Telegram DCs over the network, so this runtime can
        fail exactly like a remote provider. Anything left unmapped escapes the
        safe error envelope entirely (FastMCP masks it into an opaque string)
        and lands in the governor's generic branch, where it never records a
        transient outcome — so an unreachable Telegram would be retried forever
        without ever opening its circuit.
        """
        if isinstance(exc, TimeoutError):
            return ProviderTimeoutError("embedded Telegram runtime timed out")
        if isinstance(exc, OSError):
            return ProviderTransportError("embedded Telegram runtime transport failed")
        return ProviderError("embedded Telegram runtime failed")

    def validate_write(self, upstream_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate a write before any upstream admission or send.

        Fail-closed: only allowlisted tools with a bound account and bounded
        text pass. Runs as the governor pre-admission validator, so a rejected
        write never opens the provider circuit.
        """
        if not self.write_enabled:
            raise ProviderError("Telegram write runtime is not enabled")
        if upstream_tool not in TELEGRAM_WRITE_ALLOWLIST:
            raise ProviderError("Telegram write tool is not in the platform allowlist")
        values = dict(arguments)
        account = values.get("account")
        if not isinstance(account, str) or _ACCOUNT_LABEL.fullmatch(account) is None:
            raise ProviderError("Telegram account binding is required")
        if not self.has_account(account):
            raise ProviderError("Telegram account is not configured")
        # Bound the free-text body of the common send/reply tools; other tools
        # rely on the upstream's own @validate_id / typed argument checks.
        for field in ("message", "text", "new_text", "caption"):
            if field in values and values[field] is not None:
                self._bounded_text(values[field], field, 1, 4096)
        return getattr(self, "media_policy", MediaPolicy()).validate(upstream_tool, values)

    async def call_write(self, upstream_tool: str, arguments: dict[str, Any]) -> Any:
        values = self.validate_write(upstream_tool, arguments)
        # Same FastMCP 3 null-vs-omitted rule the read path applies in
        # ``_upstream_call``: an explicit JSON null fails validation for a
        # ``str = None`` parameter, while omitting it selects the default.
        values = {key: value for key, value in values.items() if value is not None}
        try:
            server = await self._embedded_server()
            if upstream_tool == "download_media":
                return await download(self.media_policy, self._runtime, values)
            if upstream_tool in FILE_FIELDS and "chat_id" in values and self._runtime is not None:
                await exact_chat(
                    self._runtime, self._runtime.get_client(values["account"]), values["chat_id"]
                )
            async with Client(
                FastMCPTransport(server),
                timeout=TELEGRAM_UPSTREAM_TIMEOUT_SECONDS,
                roots=[root.as_uri() for root in self.media_policy.roots],
                mode="legacy",
            ) as client:
                result = await client.call_tool(upstream_tool, values)
        except ProviderError:
            raise
        except ToolError as exc:
            raise ProviderError(_tool_error_detail("write", exc)) from exc
        except Exception as exc:
            raise self._runtime_error(exc) from exc
        if result.is_error:
            raise ProviderError("embedded Telegram write returned an error")
        error_prefixes = ("Error:", "An error occurred (code:")
        texts = [text for item in result.content if isinstance(text := getattr(item, "text", None), str)]
        if any(text.startswith(error_prefixes) for text in texts):
            raise ProviderError("embedded Telegram write returned an error")
        # Preserve structured delivery receipts while flattening FastMCP's
        # scalar ``{"result": ...}`` wrapper for legacy string-return tools.
        value = _fastmcp_result_value(result)
        if isinstance(value, str) and value.startswith(error_prefixes):
            raise ProviderError("embedded Telegram write returned an error")
        verify_receipt(upstream_tool, value)
        return value

    @staticmethod
    def _upstream_call(platform_tool: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        upstream = TELEGRAM_TOOL_MAP.get(platform_tool)
        if upstream is None:
            raise ProviderError("Telegram write or unknown tool denied by platform allowlist")
        values = dict(arguments)
        account = values.get("account")
        if not isinstance(account, str) or _ACCOUNT_LABEL.fullmatch(account) is None:
            raise ProviderError("Telegram account binding is required")
        if platform_tool in {"telegram_list_chats", "telegram_get_inbox"}:
            TelegramAdapter._bounded_integer(values.get("limit"), "limit", 1, 100)
            chat_type = values.get("chat_type")
            if chat_type is not None and chat_type not in {"user", "group", "channel"}:
                raise ProviderError("Telegram chat_type must be user, group, or channel")
            for field in ("unread_only", "unmuted_only", "with_about"):
                if field in values and not isinstance(values[field], bool):
                    raise ProviderError(f"Telegram {field} must be boolean")
            if (
                "archived" in values
                and values["archived"] is not None
                and not isinstance(values["archived"], bool)
            ):
                raise ProviderError("Telegram archived must be boolean or null")
        elif platform_tool == "telegram_search_messages":
            TelegramAdapter._bounded_text(values.get("query"), "query", 1, 512)
            TelegramAdapter._bounded_integer(values.get("limit"), "limit", 1, 100)
            if values.get("chat_id") is not None:
                TelegramAdapter._bounded_text(values.get("chat_id"), "chat_id", 1, 128)
        elif platform_tool == "telegram_get_messages":
            TelegramAdapter._bounded_text(values.get("chat_id"), "chat_id", 1, 128)
            TelegramAdapter._bounded_integer(values.get("page"), "page", 1, 10_000)
            TelegramAdapter._bounded_integer(values.get("page_size"), "page_size", 1, 100)
        elif platform_tool == "telegram_get_chat":
            TelegramAdapter._bounded_text(values.get("chat_id"), "chat_id", 1, 128)
        elif platform_tool == "telegram_get_history":
            TelegramAdapter._bounded_text(values.get("chat_id"), "chat_id", 1, 128)
            TelegramAdapter._bounded_integer(values.get("limit"), "limit", 1, 100)
        elif platform_tool == "telegram_search_contacts":
            TelegramAdapter._bounded_text(values.get("query"), "query", 1, 128)
        elif platform_tool == "telegram_search_public_chats":
            TelegramAdapter._bounded_text(values.get("query"), "query", 1, 128)
            TelegramAdapter._bounded_integer(values.get("limit"), "limit", 1, 50)
        if platform_tool == "telegram_search_messages" and values.get("chat_id") is None:
            upstream = "search_global"
            values.pop("chat_id", None)
            values["page_size"] = values.pop("limit", 20)
            values.setdefault("page", 1)
        # The pinned vendor functions use annotations such as ``str = None``.
        # FastMCP 3 generates a string-only input schema for those parameters,
        # so an explicit JSON null fails validation even though omitting the
        # argument correctly selects the function's None default.
        values = {key: value for key, value in values.items() if value is not None}
        return upstream, values

    @staticmethod
    def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ProviderError(f"Telegram {name} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _bounded_text(value: Any, name: str, minimum: int, maximum: int) -> str:
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ProviderError(f"Telegram {name} length must be between {minimum} and {maximum}")
        return value

    async def call(self, platform_tool: str, arguments: dict[str, Any]) -> Any:
        upstream, values = self._upstream_call(platform_tool, arguments)
        try:
            server = await self._embedded_server()
            async with Client(FastMCPTransport(server), timeout=TELEGRAM_UPSTREAM_TIMEOUT_SECONDS) as client:
                result = await client.call_tool(upstream, values)
        except ProviderError:
            raise
        except ToolError as exc:
            raise ProviderError(_tool_error_detail("tool", exc)) from exc
        except Exception as exc:
            raise self._runtime_error(exc) from exc
        if result.is_error:
            raise ProviderError("embedded Telegram tool returned an error")
        value = _fastmcp_result_value(result)
        error_prefixes = ("Error:", "An error occurred (code:")
        if isinstance(value, str) and value.startswith(error_prefixes):
            raise ProviderError("embedded Telegram tool returned an error")
        if any(
            isinstance(text := getattr(item, "text", None), str) and text.startswith(error_prefixes)
            for item in result.content
        ):
            raise ProviderError("embedded Telegram tool returned an error")
        return value

    async def close(self) -> None:
        if self._runtime is None:
            return
        clients = list(getattr(self._runtime, "clients", {}).values())
        await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)

    async def poll_messages(self, chat, after, limit, account):
        from zai_telegram.polling import read_since

        if not self.has_account(account):
            raise ProviderError("Telegram account is not configured")
        try:
            return await read_since(self, chat, after, limit, account)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._runtime_error(exc) from exc

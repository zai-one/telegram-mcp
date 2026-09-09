from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from zai_telegram.adapter import TelegramAdapter
from zai_telegram.errors import SafeToolError, safe_provider_error
from zai_telegram.sanitizer import sanitize_provider_response
from zai_telegram.transport import LocalPreDispatchDenied, ProviderError, ProviderRateLimited, request_hash

_TELEGRAM_BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


_TELEGRAM_RECIPIENT_REF = re.compile(r"telegram:(-?[0-9]{1,20})")


_TELEGRAM_FOREIGN_DIALOG_KEYS = frozenset({"last_message", "unread", "archived"})


def _telegram_ledger_chat_id(arguments: dict[str, Any]) -> str | None:
    """Name the destination dialog of one write for the delivery ledger.

    Most allowlisted write tools take ``chat_id``. The forward pair instead
    takes ``from_chat_id``/``to_chat_id``; the destination is what an outbox
    reader means by "where did this go", so only ``to_chat_id`` is accepted as
    a fallback. Tools addressing something other than a dialog - ``group_id``,
    ``user_id`` - deliberately record no chat rather than a misleading one.
    """
    for field in ("chat_id", "to_chat_id"):
        value = arguments.get(field)
        if value is not None:
            return str(value)
    return None


def _telegram_chat_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only chat facts the upstream resolves from the requested entity."""
    return {
        key: value
        for key, value in payload.items()
        if key != "results" and key not in _TELEGRAM_FOREIGN_DIALOG_KEYS
    }


def _telegram_json_payload(value: Any, *, empty_prefix: str) -> dict[str, Any]:
    """Decode one upstream envelope without interpreting user content.

    Two record-set encodings are accepted, because the pinned vendor emits
    both. Most read tools go through its ``format_tool_result`` and return
    ``{"results": [...]}``; ``search_public_chats`` does not, and returns a
    bare JSON array instead. A bare list can also reach us with no JSON at
    all, when ``_fastmcp_result_value`` unwraps a FastMCP 3 ``{"result": ...}``
    output model. Both mean the same thing: this is the record set.
    """
    decoded: Any
    if isinstance(value, str):
        if value.startswith(empty_prefix):
            return {"results": []}
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderError("embedded Telegram tool returned an invalid envelope") from exc
    elif isinstance(value, dict | list):
        decoded = value
    else:
        raise ProviderError("embedded Telegram tool returned an invalid envelope")
    # Normalise after decoding, not before: a JSON string carrying an array
    # lands here as a list and means exactly what a bare list means.
    payload = {"results": decoded} if isinstance(decoded, list) else decoded
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise ProviderError("embedded Telegram tool returned an invalid envelope")
    return payload


def _telegram_exact_recipient_match(query: str, candidate: dict[str, Any]) -> bool:
    normalized = query.strip().casefold()
    username = str(candidate.get("username") or "").lstrip("@").casefold()
    phone = re.sub(r"\D", "", str(candidate.get("phone") or ""))
    query_phone = re.sub(r"\D", "", query)
    name = str(candidate.get("name") or candidate.get("title") or "").strip().casefold()
    identifier = str(candidate.get("id") or candidate.get("chat_id") or "")
    return bool(
        identifier == query.strip()
        or (username and username == normalized.lstrip("@"))
        or (query_phone and phone and phone == query_phone)
        or (name and name == normalized)
    )


def _telegram_recipient_resolution(
    query: str, source_records: list[tuple[str, list[dict[str, Any]]]], limit: int
) -> dict[str, Any]:
    """Merge candidates and resolve only a unique exact identity match."""
    by_id: dict[str, dict[str, Any]] = {}
    for source, records in source_records:
        for raw in records:
            identifier = raw.get("id", raw.get("chat_id"))
            if isinstance(identifier, bool) or not isinstance(identifier, int | str):
                continue
            candidate_id = str(identifier)
            if not re.fullmatch(r"-?[0-9]{1,20}", candidate_id):
                continue
            candidate = by_id.setdefault(
                candidate_id,
                {
                    "recipient_ref": f"telegram:{candidate_id}",
                    "chat_id": candidate_id,
                    "name": raw.get("name") or raw.get("title"),
                    "username": raw.get("username"),
                    "phone": raw.get("phone"),
                    "type": str(raw.get("type") or "unknown").lower(),
                    "sources": [],
                    "untrusted_content": True,
                },
            )
            if source not in candidate["sources"]:
                candidate["sources"].append(source)
            candidate["exact_match"] = bool(candidate.get("exact_match")) or (
                _telegram_exact_recipient_match(query, raw)
            )
    ordered = sorted(
        by_id.values(),
        key=lambda item: (not bool(item.get("exact_match")), str(item.get("name") or "")),
    )
    # Uniqueness is decided over every candidate, never over the truncated page.
    # Slicing first would hide a second exact match behind ``limit`` and report
    # a confident ``unique`` for an ambiguous identity - a fail-open the write
    # path would then act on.
    exact = [candidate for candidate in ordered if candidate.get("exact_match")]
    status = "unique" if len(exact) == 1 else ("not_found" if not ordered else "ambiguous")
    ordered = ordered[:limit]
    return {
        "contract": "telegram-recipient-resolution.v1",
        "status": status,
        "recipient": exact[0] if status == "unique" else None,
        "candidates": ordered,
        "ambiguity_requires_selection": status == "ambiguous",
    }


def register_tools(server: Any, runtime: Any) -> None:
    registry, settings, store = runtime.registry, runtime.settings, runtime.store
    current_access, require_scopes = runtime.current_access, runtime.require_scopes

    async def _telegram_call(tool_name: str, arguments: dict[str, Any]) -> Any:
        access = current_access()
        try:
            account = await store.provider_binding(access.principal_id, "telegram")
            if account is None:
                raise PermissionError("Telegram account is not assigned to this principal")

            async def invoke() -> Any:
                return await registry.telegram().call(tool_name, {**arguments, "account": account})

            call_arguments = {**arguments, "account": account}
            return sanitize_provider_response(
                await registry.read(
                    access.principal_id,
                    "telegram",
                    tool_name,
                    call_arguments,
                    invoke,
                    validate=lambda: TelegramAdapter._upstream_call(tool_name, call_arguments),
                )
            )
        except (ProviderError, PermissionError, ValueError) as exc:
            raise safe_provider_error("telegram", exc) from None

    async def _telegram_write(
        upstream_tool: str,
        arguments: dict[str, Any],
        idempotency_key: str | None,
        *,
        correspondence: dict[str, str] | None = None,
    ) -> Any:
        access = current_access()
        try:
            if not settings.telegram_write_enabled:
                raise ProviderError("Telegram write runtime is not enabled")
            # The provider switch is the operational pause gate. Check it
            # before constructing the embedded adapter or reserving an
            # idempotency-ledger row, so a paused Telegram runtime has no
            # upstream or durable write side effects.
            await registry.ensure_enabled("telegram")
            account = await store.provider_binding(access.principal_id, "telegram")
            if account is None:
                raise PermissionError("Telegram account is not assigned to this principal")
            call_arguments = {**arguments, "account": account}
            adapter = registry.telegram()
            # Fail-closed validation before any reservation or admission, so a
            # bad write never opens the provider circuit or burns an idempotency
            # key.
            adapter.validate_write(upstream_tool, call_arguments)
            tier = adapter.risk_tier(upstream_tool)
            digest = request_hash({"tool": upstream_tool, "arguments": arguments})
            supplied = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
            key = supplied or digest
            is_new, cached = await store.telegram_write_reserve(
                access.principal_id,
                key,
                tool=upstream_tool,
                account_label=account,
                request_hash=digest,
                batch_id=correspondence.get("batch_id") if correspondence else None,
                item_id=correspondence.get("item_id") if correspondence else None,
                recipient_ref=correspondence.get("recipient_ref") if correspondence else None,
                chat_id=_telegram_ledger_chat_id(arguments),
            )
            if not is_new:
                assert cached is not None
                if cached.get("request_hash") != digest:
                    raise ProviderError("idempotency key was used for a different Telegram write")
                status = cached.get("status")
                if status == "sent":
                    cached_result = sanitize_provider_response(cached.get("result"))
                    if correspondence is not None:
                        return {
                            "delivery_status": "duplicate_replay",
                            "provider_result": cached_result,
                        }
                    return cached_result
                if status == "pending":
                    raise ProviderError(
                        "Telegram write is pending reconciliation; retry with a new idempotency key"
                    )
                raise ProviderError("Telegram write previously failed; retry with a new idempotency key")
            # High-risk writes (delete/admin/identity) get a dedicated, tighter
            # per-minute ceiling on top of the provider governor.
            if tier == "high" and not await store.consume_provider_request(
                access.principal_id,
                "telegram:high",
                settings.telegram_high_risk_rate_limit_per_minute,
            ):
                await store.telegram_write_settle(
                    access.principal_id,
                    key,
                    status="failed",
                    result={"error": "high_risk_rate_limited"},
                    error_code="provider_rate_limited",
                )
                raise ProviderRateLimited(
                    "Telegram high-risk write rate limit reached", retry_after_seconds=60
                )
            try:
                result = await registry.call(
                    access.principal_id,
                    "telegram",
                    upstream_tool,
                    lambda: adapter.call_write(upstream_tool, call_arguments),
                )
            except ProviderError as exc:
                # Only standalone's explicit pre-factory denial proves no send.
                # Network/upstream errors remain pending for reconciliation.
                error_code = safe_provider_error("telegram", exc).error_code
                await store.telegram_write_settle(
                    access.principal_id,
                    key,
                    status="failed" if isinstance(exc, LocalPreDispatchDenied) else "pending",
                    result=None,
                    error_code=error_code,
                )
                raise
            await store.telegram_write_settle(access.principal_id, key, status="sent", result=result)
            sanitized = sanitize_provider_response(result)
            if correspondence is not None:
                return {"delivery_status": "sent", "provider_result": sanitized}
            return sanitized
        except (ProviderError, PermissionError, ValueError) as exc:
            raise safe_provider_error("telegram", exc) from None

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_list_chats(
        limit: int = 20,
        chat_type: str | None = None,
        unread_only: bool = False,
        unmuted_only: bool = False,
        archived: bool | None = None,
        with_about: bool = False,
    ) -> Any:
        """List Telegram chats through the embedded read-only runtime."""
        return await _telegram_call(
            "telegram_list_chats",
            {
                "limit": limit,
                "chat_type": chat_type,
                "unread_only": unread_only,
                "unmuted_only": unmuted_only,
                "archived": archived,
                "with_about": with_about,
            },
        )

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_get_inbox(
        limit: int = 20,
        unread_only: bool = True,
        unmuted_only: bool = False,
        archived: bool | None = False,
        chat_type: str | None = None,
    ) -> dict[str, Any]:
        """Return a side-effect-free inbox snapshot; never marks messages read."""
        filters = {
            "limit": limit,
            "unread_only": unread_only,
            "unmuted_only": unmuted_only,
            "archived": archived,
            "chat_type": chat_type,
            "with_about": False,
        }
        raw = await _telegram_call("telegram_get_inbox", filters)
        try:
            payload = _telegram_json_payload(raw, empty_prefix="No chats found")
        except ProviderError as exc:
            raise safe_provider_error("telegram", exc) from None
        return {
            "contract": "telegram-inbox.v1",
            "filters": filters,
            "chats": payload["results"],
            "read_side_effects": False,
            "untrusted_content": True,
        }

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_search_messages(query: str, chat_id: str | None = None, limit: int = 20) -> Any:
        """Search Telegram messages without exposing write operations."""
        args: dict[str, Any] = {"query": query, "limit": limit}
        if chat_id is not None:
            args["chat_id"] = chat_id
        return await _telegram_call("telegram_search_messages", args)

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_get_messages(chat_id: str, page: int = 1, page_size: int = 20) -> Any:
        """Get a bounded page of messages from a Telegram chat."""
        return await _telegram_call(
            "telegram_get_messages", {"chat_id": chat_id, "page": page, "page_size": page_size}
        )

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_get_chat(chat_id: str) -> Any:
        """Get safe metadata for one Telegram chat."""
        raw = await _telegram_call("telegram_get_chat", {"chat_id": chat_id})
        try:
            payload = _telegram_json_payload(raw, empty_prefix="No chat found")
        except ProviderError as exc:
            raise safe_provider_error("telegram", exc) from exc
        return {"results": payload.get("results", []), **_telegram_chat_metadata(payload)}

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_get_conversation_context(chat_id: str, limit: int = 50) -> dict[str, Any]:
        """Return normalized chat metadata and chronological message envelopes."""
        chat_raw = await _telegram_call("telegram_get_chat", {"chat_id": chat_id})
        history_raw = await _telegram_call("telegram_get_history", {"chat_id": chat_id, "limit": limit})
        try:
            chat_payload = _telegram_json_payload(chat_raw, empty_prefix="No chat found")
            history_payload = _telegram_json_payload(history_raw, empty_prefix="No messages found")
        except ProviderError as exc:
            raise safe_provider_error("telegram", exc) from None
        chat = _telegram_chat_metadata(chat_payload)
        messages = [item for item in history_payload["results"] if isinstance(item, dict)]
        messages.reverse()
        if messages:
            # Newest message of the window we just read for THIS chat, which is
            # the only last-message value here that is known to be correct.
            chat["last_message"] = messages[-1]
        return {
            "contract": "telegram-conversation-context.v1",
            "chat": chat,
            "messages": messages,
            "message_order": "oldest_first",
            "untrusted_content": True,
        }

    @server.tool(auth=require_scopes("telegram:read"))
    async def telegram_resolve_recipient(
        query: str, limit: int = 20, include_public: bool = True
    ) -> dict[str, Any]:
        """Resolve a recipient fail-closed; partial or multiple matches stay ambiguous."""
        try:
            TelegramAdapter._bounded_text(query, "query", 1, 128)
            TelegramAdapter._bounded_integer(limit, "limit", 1, 50)
            if not isinstance(include_public, bool):
                raise ValueError("include_public must be boolean")
        except (ProviderError, ValueError) as exc:
            raise safe_provider_error("telegram", exc) from None
        sources: list[tuple[str, list[dict[str, Any]]]] = []
        requests: list[tuple[str, str, dict[str, Any], str]] = [
            ("contacts", "telegram_search_contacts", {"query": query}, "No contacts found"),
            (
                "dialogs",
                "telegram_get_inbox",
                {
                    "limit": 100,
                    "unread_only": False,
                    "unmuted_only": False,
                    "archived": None,
                    "chat_type": None,
                    "with_about": False,
                },
                "No chats found",
            ),
        ]
        if include_public:
            requests.append(
                (
                    "public",
                    "telegram_search_public_chats",
                    {"query": query, "limit": limit},
                    "No public chats found",
                )
            )
        # ``public`` is an optional widening source: contacts and dialogs alone
        # already resolve everyone the account can message. Letting its failure
        # abort the whole call made the tool unusable on its own default,
        # because ``_telegram_call`` raises SafeToolError - not ProviderError -
        # so the handler below never caught it either.
        degraded_sources: list[str] = []
        incomplete_sources: list[str] = []
        for source, tool, arguments, empty_prefix in requests:
            try:
                raw = await _telegram_call(tool, arguments)
                payload = _telegram_json_payload(raw, empty_prefix=empty_prefix)
                records = [item for item in payload["results"] if isinstance(item, dict)]
                total = payload.get("total_count", payload.get("total"))
                if (
                    payload.get("has_more") is True
                    or payload.get("truncated") is True
                    or payload.get("completeness") == "partial"
                    or bool(payload.get("next_cursor"))
                    or (isinstance(total, int) and not isinstance(total, bool) and total > len(records))
                    or (source == "dialogs" and len(records) >= 100)
                    or (source == "public" and len(records) >= limit)
                ):
                    incomplete_sources.append(source)
            except (ProviderError, SafeToolError) as exc:
                if source != "public":
                    if isinstance(exc, SafeToolError):
                        raise
                    raise safe_provider_error("telegram", exc) from exc
                degraded_sources.append(source)
                records = []
            sources.append((source, records))
        resolution = _telegram_recipient_resolution(query, sources, limit)
        if incomplete_sources:
            resolution.update(
                status="ambiguous",
                recipient=None,
                ambiguity_requires_selection=True,
                incomplete_sources=incomplete_sources,
            )
        if degraded_sources:
            # Named, not hidden: a caller that needed the public directory must
            # be able to tell a real "not found" from a source that was down.
            resolution["degraded_sources"] = degraded_sources
        return resolution

    @server.tool(auth=require_scopes("telegram:write"))
    async def telegram_send_message(
        chat_id: str,
        message: str,
        parse_mode: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a Telegram message autonomously (idempotent, audited)."""
        arguments: dict[str, Any] = {"chat_id": chat_id, "message": message}
        if parse_mode is not None:
            arguments["parse_mode"] = parse_mode
        return await _telegram_write("send_message", arguments, idempotency_key)

    @server.tool(auth=require_scopes("telegram:write"))
    async def telegram_reply_to_message(
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Reply to a Telegram message in a dialog (idempotent, audited)."""
        arguments: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode is not None:
            arguments["parse_mode"] = parse_mode
        return await _telegram_write("reply_to_message", arguments, idempotency_key)

    @server.tool(auth=require_scopes("telegram:write"))
    async def telegram_send_many(batch_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Send 1..20 independently idempotent messages to resolved recipients."""
        try:
            if _TELEGRAM_BATCH_ID.fullmatch(batch_id) is None:
                raise ValueError("batch_id must be a stable 1..128 character identifier")
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError("items must contain between 1 and 20 messages")
            normalized: list[dict[str, str | None]] = []
            item_ids: set[str] = set()
            idempotency_keys: set[str] = set()
            allowed = {
                "item_id",
                "recipient_ref",
                "message",
                "parse_mode",
                "idempotency_key",
            }
            for raw in items:
                if not isinstance(raw, dict) or set(raw) - allowed:
                    raise ValueError("each batch item must use only the documented fields")
                item_id = raw.get("item_id")
                recipient_ref = raw.get("recipient_ref")
                message = raw.get("message")
                parse_mode = raw.get("parse_mode")
                idempotency_key = raw.get("idempotency_key")
                if not isinstance(item_id, str) or _TELEGRAM_BATCH_ID.fullmatch(item_id) is None:
                    raise ValueError("each item_id must be a stable 1..128 character identifier")
                recipient_match = (
                    _TELEGRAM_RECIPIENT_REF.fullmatch(recipient_ref)
                    if isinstance(recipient_ref, str)
                    else None
                )
                if recipient_match is None:
                    raise ValueError("each recipient_ref must come from telegram_resolve_recipient")
                TelegramAdapter._bounded_text(message, "message", 1, 4096)
                if parse_mode not in {None, "html", "md", "markdown"}:
                    raise ValueError("parse_mode must be html, md, markdown, or null")
                if (
                    not isinstance(idempotency_key, str)
                    or _TELEGRAM_BATCH_ID.fullmatch(idempotency_key) is None
                ):
                    raise ValueError("each item requires a stable idempotency_key")
                if item_id in item_ids or idempotency_key in idempotency_keys:
                    raise ValueError("item_id and idempotency_key must be unique within a batch")
                item_ids.add(item_id)
                idempotency_keys.add(idempotency_key)
                normalized.append(
                    {
                        "item_id": item_id,
                        "recipient_ref": recipient_ref,
                        "chat_id": recipient_match.group(1),
                        "message": message,
                        "parse_mode": parse_mode,
                        "idempotency_key": idempotency_key,
                    }
                )
        except (ProviderError, ValueError) as exc:
            raise safe_provider_error("telegram", exc) from None

        receipts: list[dict[str, Any]] = []
        access = current_access()
        for item in normalized:
            arguments: dict[str, Any] = {
                "chat_id": item["chat_id"],
                "message": item["message"],
            }
            if item["parse_mode"] is not None:
                arguments["parse_mode"] = item["parse_mode"]
            correspondence = {
                "batch_id": batch_id,
                "item_id": str(item["item_id"]),
                "recipient_ref": str(item["recipient_ref"]),
            }
            try:
                outcome = await _telegram_write(
                    "send_message",
                    arguments,
                    str(item["idempotency_key"]),
                    correspondence=correspondence,
                )
                provider_result = outcome.get("provider_result") if isinstance(outcome, dict) else None
                message_ids: list[int] = []
                if isinstance(provider_result, dict):
                    raw_ids = provider_result.get("message_ids")
                    if isinstance(raw_ids, list):
                        message_ids = [value for value in raw_ids if isinstance(value, int)]
                    elif isinstance(provider_result.get("message_id"), int):
                        message_ids = [provider_result["message_id"]]
                receipts.append(
                    {
                        "batch_id": batch_id,
                        "item_id": item["item_id"],
                        "recipient_ref": item["recipient_ref"],
                        "idempotency_key": item["idempotency_key"],
                        "status": outcome.get("delivery_status", "sent"),
                        "message_ids": message_ids,
                        "reconciliation_required": False,
                    }
                )
            except SafeToolError as exc:
                rows = await store.telegram_write_list(
                    access.principal_id,
                    idempotency_keys=[str(item["idempotency_key"])],
                    limit=1,
                )
                ledger_status = rows[0].get("status") if rows else "failed"
                receipts.append(
                    {
                        "batch_id": batch_id,
                        "item_id": item["item_id"],
                        "recipient_ref": item["recipient_ref"],
                        "idempotency_key": item["idempotency_key"],
                        "status": ("pending_reconciliation" if ledger_status == "pending" else "failed"),
                        "message_ids": [],
                        "error_code": exc.error_code,
                        "reconciliation_required": ledger_status == "pending",
                    }
                )
        counts: dict[str, int] = {}
        for receipt in receipts:
            state = str(receipt["status"])
            counts[state] = counts.get(state, 0) + 1
        return {
            "contract": "telegram-outreach-batch.v1",
            "batch_id": batch_id,
            "atomic": False,
            "receipts": receipts,
            "summary": counts,
        }

    @server.tool(auth=require_scopes("telegram:write"))
    async def telegram_outbox_status(
        batch_id: str | None = None,
        idempotency_keys: list[str] | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read the authenticated principal's durable Telegram delivery ledger."""
        try:
            if batch_id is not None and _TELEGRAM_BATCH_ID.fullmatch(batch_id) is None:
                raise ValueError("batch_id is invalid")
            if idempotency_keys is not None:
                if not 1 <= len(idempotency_keys) <= 100 or any(
                    not isinstance(key, str) or _TELEGRAM_BATCH_ID.fullmatch(key) is None
                    for key in idempotency_keys
                ):
                    raise ValueError("idempotency_keys must contain 1..100 stable identifiers")
                idempotency_keys = list(dict.fromkeys(idempotency_keys))
            if status not in {None, "pending", "sent", "failed"}:
                raise ValueError("status must be pending, sent, failed, or null")
            TelegramAdapter._bounded_integer(limit, "limit", 1, 100)
        except (ProviderError, ValueError) as exc:
            raise safe_provider_error("telegram", exc) from None
        access = current_access()
        rows = await store.telegram_write_list(
            access.principal_id,
            batch_id=batch_id,
            idempotency_keys=idempotency_keys,
            status=status,
            limit=limit,
        )
        records = []
        for row in rows:
            provider_result = row.get("result")
            message_ids: list[int] = []
            if isinstance(provider_result, dict):
                raw_ids = provider_result.get("message_ids")
                if isinstance(raw_ids, list):
                    message_ids = [value for value in raw_ids if isinstance(value, int)]
                elif isinstance(provider_result.get("message_id"), int):
                    message_ids = [provider_result["message_id"]]
            created_at = row.get("created_at")
            settled_at = row.get("settled_at")
            records.append(
                {
                    "batch_id": row.get("batch_id"),
                    "item_id": row.get("item_id"),
                    "recipient_ref": row.get("recipient_ref"),
                    "chat_id": row.get("chat_id"),
                    "idempotency_key": row.get("idempotency_key"),
                    "tool": row.get("tool"),
                    "status": row.get("status"),
                    "message_ids": message_ids,
                    "error_code": row.get("error_code"),
                    "reconciliation_required": row.get("status") == "pending",
                    "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
                    "settled_at": settled_at.isoformat() if isinstance(settled_at, datetime) else settled_at,
                }
            )
        return {"contract": "telegram-delivery-outbox.v1", "records": records}

    @server.tool(auth=require_scopes("telegram:write"))
    async def telegram_write(
        tool: str,
        arguments: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> Any:
        """Run any allowlisted Telegram write tool by name (idempotent, audited).

        `tool` must be an entry in the frozen platform write allowlist; unknown
        or non-allowlisted names are refused. `arguments` are the upstream tool
        parameters minus `account`, which the platform injects from the
        principal binding.
        """
        if not isinstance(arguments, dict):
            raise safe_provider_error("telegram", ValueError("arguments must be an object"))
        payload = {key: value for key, value in arguments.items() if key != "account"}
        return await _telegram_write(tool, payload, idempotency_key)

"""Bounded polling with explicit, durable acknowledgment of a delivered batch."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from uuid import uuid4

from zai_telegram.media_policy import exact_chat
from zai_telegram.transport import ProviderError

WAIT_MAX_SECONDS = 55
WAIT_MIN_INTERVAL = 5.0
SETTLE_MIN_SECONDS = 1
SETTLE_MAX_SECONDS = 30
_sleep = asyncio.sleep
_monotonic = time.monotonic


class PollStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_poll_cursors(
                    account TEXT NOT NULL, actor TEXT NOT NULL, label TEXT NOT NULL,
                    chat TEXT NOT NULL, message_id INTEGER NOT NULL,
                    PRIMARY KEY(account,actor,label,chat));
                CREATE TABLE IF NOT EXISTS telegram_poll_batches(
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, actor TEXT NOT NULL,
                    label TEXT NOT NULL, proposal TEXT NOT NULL, expires REAL NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0);
            """)

    def identity(self, actor):
        label = self.store.config.resolve_account(actor)
        if label is None:
            raise PermissionError("bound account required")
        return self.store.account, actor, label

    def cursors(self, actor, chats):
        identity = self.identity(actor)
        with self.store.connect() as db:
            result = {}
            for chat in chats:
                row = db.execute(
                    "SELECT message_id FROM telegram_poll_cursors WHERE account=? AND actor=? "
                    "AND label=? AND chat=?",
                    (*identity, chat),
                ).fetchone()
                result[chat] = row[0] if row else None
            return result

    def prepare(self, actor, proposal):
        identity = self.identity(actor)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("DELETE FROM telegram_poll_batches WHERE expires<?", (now,))
            count = db.execute(
                "SELECT count(*) FROM telegram_poll_batches WHERE account=?", (identity[0],)
            ).fetchone()[0]
            if count >= 1000:
                raise ProviderError("too many pending poll batches; wait for expiry")
            batch = uuid4().hex
            db.execute(
                "INSERT INTO telegram_poll_batches VALUES(?,?,?,?,?,?,0)",
                (batch, *identity, json.dumps(proposal), now + 86400),
            )
            return batch

    def acknowledge(self, actor, batch):
        if not isinstance(batch, str) or re.fullmatch(r"[a-f0-9]{32}", batch) is None:
            raise ValueError("poll batch ID required")
        identity = self.identity(actor)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM telegram_poll_batches WHERE id=? AND account=? AND actor=? AND label=?",
                (batch, *identity),
            ).fetchone()
            if row is None or row["expires"] < time.time():
                raise ProviderError("poll batch not found or expired; poll again")
            if row["acknowledged"]:
                return {"batch_id": batch, "acknowledged": True, "replayed": True}
            proposal = json.loads(row["proposal"])
            for chat, (before, after) in proposal.items():
                current = db.execute(
                    "SELECT message_id FROM telegram_poll_cursors WHERE account=? AND actor=? "
                    "AND label=? AND chat=?",
                    (*identity, chat),
                ).fetchone()
                if (current[0] if current else None) != before:
                    raise ProviderError("poll cursor changed; poll again before acknowledging")
                db.execute(
                    "INSERT INTO telegram_poll_cursors VALUES(?,?,?,?,?) "
                    "ON CONFLICT(account,actor,label,chat) DO UPDATE SET message_id=excluded.message_id",
                    (*identity, chat, after),
                )
            db.execute("UPDATE telegram_poll_batches SET acknowledged=1 WHERE id=?", (batch,))
        return {"batch_id": batch, "acknowledged": True, "replayed": False}


async def read_since(adapter, chat, after, limit, account):
    """Use the pinned runtime's bound client; never its cross-account event buffer."""
    await adapter._embedded_server()
    runtime = adapter._runtime
    client = runtime.get_client(account)
    entity = await exact_chat(runtime, client, chat)
    kwargs = {"limit": 1} if after is None else {"min_id": after, "reverse": True, "limit": limit + 1}
    messages = await client.get_messages(entity, **kwargs)
    rows = []
    for message in messages:
        identifier = getattr(message, "id", None)
        if type(identifier) is not int or not 1 <= identifier <= 2147483647:
            raise ProviderError("invalid message identity in poll response")
        text = getattr(message, "message", None) or ""
        if not isinstance(text, str):
            raise ProviderError("invalid message text")
        date = getattr(message, "date", None)
        rows.append(
            {
                "message_id": identifier,
                "text": text[:4096],
                "text_truncated": len(text) > 4096,
                "date": date.isoformat() if isinstance(date, datetime) else None,
                "outgoing": bool(getattr(message, "out", False)),
                "has_media": bool(getattr(message, "media", None)),
                "untrusted_content": True,
            }
        )
    return rows


def register_polling(server, runtime):
    store = PollStore(runtime.store)

    # Polling never sends read receipts; it only records a local cursor batch.
    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    async def telegram_poll_messages(chat_ids: list[str], max_per_chat: int = 20) -> dict:
        """Poll selected numeric chats after acknowledged IDs; first poll proposes a baseline at now."""
        return await _poll(chat_ids, max_per_chat)

    async def _poll(chat_ids: list[str], max_per_chat: int) -> dict:
        if not 1 <= len(chat_ids) <= 10 or len(set(chat_ids)) != len(chat_ids):
            raise ValueError("provide 1 to 10 distinct numeric chat IDs")
        if any(re.fullmatch(r"-?[1-9][0-9]{0,18}", chat) is None for chat in chat_ids):
            raise ValueError("explicit numeric chat IDs required")
        if type(max_per_chat) is not int or not 1 <= max_per_chat <= 50:
            raise ValueError("max_per_chat must be between 1 and 50")
        actor = runtime.current_access().principal_id
        account = runtime.config.resolve_account(actor)
        cursors = store.cursors(actor, chat_ids)
        reports, proposal = [], {}
        for chat, before in cursors.items():
            values = await runtime.registry.read(
                actor,
                "telegram",
                "poll_messages",
                {"chat_id": chat, "after": before},
                lambda chat=chat, before=before: runtime.adapter.poll_messages(
                    chat, before, max_per_chat, account
                ),
            )
            if not isinstance(values, list) or len(values) > (1 if before is None else max_per_chat + 1):
                raise ProviderError("unexpected poll response size")
            ids = [row.get("message_id") for row in values if isinstance(row, dict)]
            if len(ids) != len(values) or any(type(i) is not int or not 1 <= i <= 2147483647 for i in ids):
                raise ProviderError("invalid message identity in poll response")
            if ids != sorted(set(ids)) or (before is not None and any(i <= before for i in ids)):
                raise ProviderError("poll response overlaps or is not ordered")
            selected = values[:max_per_chat] if before is not None else []
            after = (
                (selected[-1]["message_id"] if selected else before)
                if before is not None
                else (ids[-1] if ids else 0)
            )
            proposal[chat] = [before, after]
            reports.append(
                {
                    "chat_id": chat,
                    "after_message_id": before,
                    "proposed_message_id": after,
                    "baseline_only": before is None,
                    "messages": selected,
                    "has_more": len(values) > max_per_chat if before is not None else False,
                }
            )
        result = runtime.clean(
            {
                "reports": reports,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "acknowledgment_required": True,
                "mode": "polling",
                "limitations": "New message IDs only; edits/deletions are not tracked. Not a live stream.",
            }
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > 262000:
            raise ValueError("poll batch too large; reduce chats or max_per_chat")
        # Persist only IDs and account metadata, never message bodies.
        result["batch_id"] = store.prepare(actor, proposal)
        result["expires_in_seconds"] = 86400
        return result

    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def telegram_acknowledge_poll(batch_id: str) -> dict:
        """Acknowledge a processed local poll batch without sending Telegram read receipts."""
        await runtime.registry.ensure_enabled("telegram")
        return store.acknowledge(runtime.current_access().principal_id, batch_id)

    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    async def telegram_wait_for_new_message(
        chat_ids: list[str], timeout_seconds: int = 30, max_per_chat: int = 20
    ) -> dict:
        """Bounded long-poll: block until a new message arrives in the chats or the timeout ends."""
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= WAIT_MAX_SECONDS:
            raise ValueError(f"timeout_seconds must be between 1 and {WAIT_MAX_SECONDS}")
        deadline = _monotonic() + timeout_seconds
        actor = runtime.current_access().principal_id
        # Chats without a cursor get a local baseline at "now" first, so the
        # wait only reports messages that arrive after the call started.
        result = await _poll(chat_ids, max_per_chat)
        if all(report["baseline_only"] for report in result["reports"]) or not any(
            report["messages"] for report in result["reports"]
        ):
            if any(report["baseline_only"] for report in result["reports"]):
                store.acknowledge(actor, result["batch_id"])
            # Spread reads so a wait never exceeds ~24 provider reads per minute.
            interval = max(WAIT_MIN_INTERVAL, 2.5 * len(chat_ids))
            while True:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return {"status": "timeout", "waited_seconds": timeout_seconds, "chat_ids": chat_ids}
                await _sleep(min(interval, remaining))
                if _monotonic() >= deadline:
                    return {"status": "timeout", "waited_seconds": timeout_seconds, "chat_ids": chat_ids}
                result = await _poll(chat_ids, max_per_chat)
                if any(report["messages"] for report in result["reports"]):
                    break
                # Nothing new: keep the cursors where they are (no ack needed).
        result["reports"] = [report for report in result["reports"] if report["messages"]]
        result["status"] = "new_messages"
        result["mode"] = "wait"
        return result

    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    async def telegram_wait_for_settled_message(
        chat_ids: list[str],
        settle_seconds: int = 6,
        max_wait_seconds: int = 50,
        max_per_chat: int = 20,
    ) -> dict:
        """Debounced wait: block until a chat goes quiet for settle_seconds after new messages, or max_wait ends."""
        if type(settle_seconds) is not int or not SETTLE_MIN_SECONDS <= settle_seconds <= SETTLE_MAX_SECONDS:
            raise ValueError(
                f"settle_seconds must be between {SETTLE_MIN_SECONDS} and {SETTLE_MAX_SECONDS}"
            )
        if type(max_wait_seconds) is not int or not 1 <= max_wait_seconds <= WAIT_MAX_SECONDS:
            raise ValueError(f"max_wait_seconds must be between 1 and {WAIT_MAX_SECONDS}")
        started = _monotonic()
        deadline = started + max_wait_seconds
        actor = runtime.current_access().principal_id
        interval = max(WAIT_MIN_INTERVAL, 2.5 * len(chat_ids))
        # Local baseline first (same as wait_for_new_message); do not ack later polls so
        # one batch_id covers the whole burst for the caller.
        result = await _poll(chat_ids, max_per_chat)
        if any(report["baseline_only"] for report in result["reports"]):
            store.acknowledge(actor, result["batch_id"])
            result = {"reports": [], "batch_id": None}
        seen: dict[str, set[int]] = {chat: set() for chat in chat_ids}
        last_new_at: float | None = None
        message_count = 0
        settled = False

        def ingest(poll: dict) -> bool:
            nonlocal last_new_at, message_count
            grew = False
            for report in poll["reports"]:
                chat = report["chat_id"]
                for row in report["messages"]:
                    mid = row["message_id"]
                    if mid not in seen[chat]:
                        seen[chat].add(mid)
                        message_count += 1
                        grew = True
            if grew:
                last_new_at = _monotonic()
            return grew

        if any(report.get("messages") for report in result.get("reports", [])):
            ingest(result)
        while True:
            now = _monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            if last_new_at is None:
                # Still waiting for the first new message — pace polls (~24 reads/min).
                await _sleep(min(interval, remaining))
                if _monotonic() >= deadline:
                    break
                result = await _poll(chat_ids, max_per_chat)
                ingest(result)
                continue
            until_settle = settle_seconds - (now - last_new_at)
            if until_settle > 0:
                await _sleep(min(until_settle, remaining))
                if _monotonic() >= deadline:
                    break
            # Quiet window elapsed (or zero): confirm no newer messages landed.
            result = await _poll(chat_ids, max_per_chat)
            if ingest(result):
                continue
            settled = True
            break

        waited = round(min(_monotonic() - started, float(max_wait_seconds)), 3)
        if message_count == 0:
            return {
                "status": "timeout",
                "waited_seconds": waited,
                "settle_seconds": settle_seconds,
                "chat_ids": chat_ids,
                "message_count": 0,
            }
        result["reports"] = [report for report in result["reports"] if report["messages"]]
        result["status"] = "settled" if settled else "partial"
        result["mode"] = "wait_settled"
        result["settle_seconds"] = settle_seconds
        result["waited_seconds"] = waited
        result["message_count"] = message_count
        return result

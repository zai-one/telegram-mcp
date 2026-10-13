"""Event feed: new / edited / deleted messages from Telethon update events.

Unlike the cursor polls in ``polling.py`` (one history read per chat, new IDs
only) this subscribes once per account to Telethon's ``NewMessage``,
``MessageEdited`` and ``MessageDeleted`` events and keeps a bounded in-memory
ring per account. ``telegram_poll_events`` returns the events after an opaque
cursor; the cursor is client-held, so nothing is persisted and message bodies
never reach disk. A restart or ring overflow is reported as ``gap=true`` so the
caller can re-read history instead of silently missing events.

Boundaries: one ring per account label (multi-account safe; a cursor names its
account), events are filtered by the chat ACL at read time, and deletions whose
chat Telegram does not report (private chats / basic groups) are hidden while
any chat ACL is active (fail closed).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import deque
from datetime import datetime
from typing import Any

from zai_telegram.transport import ProviderError

RING_SIZE = 2000
TEXT_LIMIT = 1000
WAIT_MAX_SECONDS = 55
KINDS = ("new", "edited", "deleted")
_CURSOR = re.compile(r"([a-z][a-z0-9_]{0,31})\.([0-9a-f]{8})\.([0-9]{1,12})")


class AccountFeed:
    def __init__(self, label: str):
        self.label = label
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
        self.changed = asyncio.Event()

    def cursor(self, seq: int | None = None) -> str:
        return f"{self.label}.{self.epoch}.{self.seq if seq is None else seq}"

    def push(self, record: dict[str, Any]) -> None:
        self.seq += 1
        self.ring.append({"seq": self.seq, **record})
        self.changed.set()


def _message_record(kind: str, message: Any) -> dict[str, Any]:
    text = getattr(message, "message", None) or ""
    if not isinstance(text, str):
        text = ""
    date = getattr(message, "edit_date", None) if kind == "edited" else getattr(message, "date", None)
    return {
        "kind": kind,
        "chat_id": getattr(message, "chat_id", None),
        "message_id": getattr(message, "id", None),
        "sender_id": getattr(message, "sender_id", None),
        "date": date.isoformat() if isinstance(date, datetime) else None,
        "outgoing": bool(getattr(message, "out", False)),
        "has_media": bool(getattr(message, "media", None)),
        "text": text[:TEXT_LIMIT],
        "text_truncated": len(text) > TEXT_LIMIT,
        "untrusted_content": True,
    }


class EventFeed:
    """Per-process registry of account feeds; handlers attach once per account."""

    def __init__(self) -> None:
        self.feeds: dict[str, AccountFeed] = {}

    def _attach(self, label: str, client: Any) -> AccountFeed:
        from telethon import events

        feed = AccountFeed(label)

        async def on_new(event: Any) -> None:
            feed.push(_message_record("new", event.message))

        async def on_edit(event: Any) -> None:
            feed.push(_message_record("edited", event.message))

        async def on_delete(event: Any) -> None:
            ids = [i for i in (getattr(event, "deleted_ids", None) or []) if type(i) is int]
            feed.push(
                {
                    "kind": "deleted",
                    "chat_id": getattr(event, "chat_id", None),
                    "message_ids": ids[:100],
                }
            )

        client.add_event_handler(on_new, events.NewMessage())
        client.add_event_handler(on_edit, events.MessageEdited())
        client.add_event_handler(on_delete, events.MessageDeleted())
        self.feeds[label] = feed
        return feed

    async def feed_for(self, adapter: Any, label: str) -> AccountFeed:
        feed = self.feeds.get(label)
        if feed is not None:
            return feed
        loader = getattr(adapter, "_embedded_server", None)
        if loader is not None:
            await loader()
        source = getattr(adapter, "event_client", None)
        if source is None:
            raise ProviderError("Telegram event feed is not available for this adapter")
        client = source(label)
        if label in self.feeds:  # attached concurrently while loading
            return self.feeds[label]
        feed = self._attach(label, client)
        connect = getattr(client, "is_connected", None)
        if callable(connect) and not connect():
            await client.connect()
        # Telegram starts pushing updates to a session after its first request.
        ping = getattr(client, "get_me", None)
        if callable(ping):
            await ping(input_peer=True)
        return feed

    @staticmethod
    def select(
        feed: AccountFeed,
        after: int,
        *,
        chats: set[str] | None,
        kinds: set[str],
        limit: int,
        permits: Any,
        acl_active: bool,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Events after ``after``; returns (events, last examined seq, more pending)."""
        out: list[dict[str, Any]] = []
        last = after
        for record in feed.ring:
            if record["seq"] <= after:
                continue
            if len(out) >= limit:
                return out, last, True
            last = record["seq"]
            chat = record.get("chat_id")
            if record["kind"] not in kinds:
                continue
            if chat is None:
                if acl_active or chats is not None:
                    continue
            else:
                if chats is not None and str(chat) not in chats:
                    continue
                if not permits(chat):
                    continue
            out.append(dict(record))
        return out, last, False


def parse_cursor(cursor: Any) -> tuple[str, str, int] | None:
    if cursor is None or cursor == "":
        return None
    match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValueError("cursor must be a next_cursor value returned by telegram_poll_events")
    return match.group(1), match.group(2), int(match.group(3))


def register_feed(server: Any, runtime: Any, surface: Any) -> None:
    registry = EventFeed()
    runtime.event_feed = registry

    @server.tool(
        auth=runtime.require_scopes("telegram:read"),
        annotations={"readOnlyHint": True, "openWorldHint": True},
    )
    async def telegram_poll_events(
        cursor: str | None = None,
        chat_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        limit: int = 50,
        wait_seconds: int = 0,
    ) -> dict:
        """Live event feed: new/edited/deleted messages after cursor (first call starts it)."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= WAIT_MAX_SECONDS:
            raise ValueError(f"wait_seconds must be between 0 and {WAIT_MAX_SECONDS}")
        wanted = set(kinds or KINDS)
        if not wanted <= set(KINDS):
            raise ValueError("kinds must be a subset of new, edited, deleted")
        if chat_ids is not None and (
            not 1 <= len(chat_ids) <= 50
            or any(not isinstance(c, str) or re.fullmatch(r"-?[0-9]{1,20}", c) is None for c in chat_ids)
        ):
            raise ValueError("chat_ids must be 1-50 numeric chat ids")
        parsed = parse_cursor(cursor)
        actor = runtime.current_access().principal_id
        label = runtime.config.resolve_account(actor)
        if label is None:
            raise PermissionError("bound account required")
        await runtime.registry.ensure_enabled("telegram")
        feed = await registry.feed_for(runtime.adapter, label)
        if parsed is not None and parsed[0] != label:
            raise ValueError("cursor belongs to another account; pass the matching account")
        if parsed is None:
            return {
                "status": "started",
                "events": [],
                "next_cursor": feed.cursor(),
                "gap": False,
                "buffer": RING_SIZE,
                "hint": "Call again with next_cursor; wait_seconds<=55 to block for events.",
            }
        gap = parsed[1] != feed.epoch
        after = 0 if gap else parsed[2]
        if not gap and after > feed.seq:
            raise ValueError("cursor is ahead of the feed")
        oldest = feed.ring[0]["seq"] if feed.ring else feed.seq + 1
        if not gap and after < oldest - 1:
            gap = True
        filters = {
            "chats": set(chat_ids) if chat_ids is not None else None,
            "kinds": wanted,
            "limit": limit,
            "permits": surface.chats.permits,
            "acl_active": surface.chats.active,
        }
        events, last, more = EventFeed.select(feed, after, **filters)
        if not events and not more and wait_seconds:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_seconds
            while not events and not more:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                feed.changed.clear()
                if feed.seq <= last:
                    try:
                        await asyncio.wait_for(feed.changed.wait(), remaining)
                    except TimeoutError:
                        break
                events, last, more = EventFeed.select(feed, last, **filters)
        return runtime.clean(
            {
                "status": "events" if events else "empty",
                "events": events,
                "next_cursor": feed.cursor(last),
                "has_more": more,
                "gap": gap,
                "account": label,
            }
        )

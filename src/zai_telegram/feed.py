"""Event feed: new / edited / deleted messages from Telethon update events.

Unlike the cursor polls in ``polling.py`` (one history read per chat, new IDs
only) this subscribes once per account to Telethon's ``NewMessage``,
``MessageEdited`` and ``MessageDeleted`` events and keeps a bounded in-memory
ring per account. ``telegram_poll_events`` returns the events after an opaque
cursor held by the client; message bodies never reach disk. A ring overflow
or lost state is reported as ``gap=true`` so the caller can re-read history
instead of silently missing events.

Boundaries: one ring per account label (multi-account safe; a cursor names its
account), events are filtered by the chat ACL at read time, and deletions whose
chat Telegram does not report (private chats / basic groups) are hidden while
any chat ACL is active (fail closed).

Restart persistence (ids only, like the poll cursors): the feed epoch, sequence
and event identities (kind, chat, message ids, dates; never text) are kept in
the local state database for 24 h. After a restart a cursor stays valid, events
seen before the shutdown are still delivered (``text_unavailable``), and only
the downtime is reported as a bounded ``gap_window`` instead of losing the
whole ring. ``TELEGRAM_FEED_PERSIST=0`` turns this off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from zai_telegram.errors import policy_error
from zai_telegram.transport import ProviderError

RING_SIZE = 2000
TEXT_LIMIT = 1000
WAIT_MAX_SECONDS = 55
KINDS = ("new", "edited", "deleted")
_CURSOR = re.compile(r"([a-z][a-z0-9_]{0,31})\.([0-9a-f]{8})\.([0-9]{1,12})")


PERSIST_TTL_SECONDS = 86400
_ID_FIELDS = (
    "kind",
    "chat_id",
    "message_id",
    "message_ids",
    "sender_id",
    "date",
    "outgoing",
    "has_media",
    "backfilled",
)


class FeedStore:
    """Ids-only persistence of feed position and event identities (no message text)."""

    def __init__(self, store: Any):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_feed_meta(
                    namespace TEXT NOT NULL, label TEXT NOT NULL, epoch TEXT NOT NULL,
                    seq INTEGER NOT NULL, alive_at REAL NOT NULL, PRIMARY KEY(namespace,label));
                CREATE TABLE IF NOT EXISTS telegram_feed_events(
                    namespace TEXT NOT NULL, label TEXT NOT NULL, seq INTEGER NOT NULL,
                    record TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(namespace,label,seq));
            """)

    def load(self, label: str) -> tuple[str, int, float, list[dict[str, Any]]] | None:
        namespace = self.store.account
        with self.store.connect() as db:
            row = db.execute(
                "SELECT epoch, seq, alive_at FROM telegram_feed_meta WHERE namespace=? AND label=?",
                (namespace, label),
            ).fetchone()
            if row is None or row["alive_at"] < time.time() - PERSIST_TTL_SECONDS:
                return None
            events = [
                {"seq": r["seq"], **json.loads(r["record"]), "text": None, "text_unavailable": True}
                for r in db.execute(
                    "SELECT seq, record FROM telegram_feed_events WHERE namespace=? AND label=? "
                    "AND created>=? ORDER BY seq DESC LIMIT ?",
                    (namespace, label, time.time() - PERSIST_TTL_SECONDS, RING_SIZE),
                )
            ]
        return row["epoch"], row["seq"], row["alive_at"], events[::-1]

    def save(self, label: str, epoch: str, record: dict[str, Any] | None, seq: int) -> None:
        namespace, now = self.store.account, time.time()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO telegram_feed_meta VALUES(?,?,?,?,?) ON CONFLICT(namespace,label) "
                "DO UPDATE SET epoch=excluded.epoch, seq=excluded.seq, alive_at=excluded.alive_at",
                (namespace, label, epoch, seq, now),
            )
            if record is not None:
                ids = {key: record[key] for key in _ID_FIELDS if key in record}
                db.execute(
                    "INSERT OR REPLACE INTO telegram_feed_events VALUES(?,?,?,?,?)",
                    (namespace, label, seq, json.dumps(ids), now),
                )
                db.execute(
                    "DELETE FROM telegram_feed_events WHERE namespace=? AND label=? "
                    "AND (seq<=? OR created<?)",
                    (namespace, label, seq - RING_SIZE, now - PERSIST_TTL_SECONDS),
                )


class AccountFeed:
    def __init__(self, label: str, persist: FeedStore | None = None):
        self.label = label
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
        self.changed = asyncio.Event()
        self.persist = persist
        self.boot_seq = 0
        self.downtime: dict[str, Any] | None = None
        self.window: tuple[float, float] | None = None
        if persist is not None:
            loaded = persist.load(label)
            if loaded is not None:
                self.epoch, self.seq, alive_at, events = loaded
                self.ring.extend(events)
                self.window = (alive_at, time.time())
                self.downtime = {
                    "from": datetime.fromtimestamp(alive_at, UTC).isoformat(),
                    "to": datetime.now(UTC).isoformat(),
                }
            self.boot_seq = self.seq
            persist.save(label, self.epoch, None, self.seq)
        self.touched = time.monotonic()

    def touch(self) -> None:
        """Keep ``alive_at`` fresh on quiet accounts (bounds the next downtime window)."""
        if self.persist is not None and time.monotonic() - self.touched > 60:
            self.touched = time.monotonic()
            with contextlib.suppress(Exception):
                self.persist.save(self.label, self.epoch, None, self.seq)

    def cursor(self, seq: int | None = None) -> str:
        return f"{self.label}.{self.epoch}.{self.seq if seq is None else seq}"

    def push(self, record: dict[str, Any]) -> None:
        self.seq += 1
        self.ring.append({"seq": self.seq, **record})
        if self.persist is not None:
            # Best effort: the live ring stays authoritative if the disk write fails.
            with contextlib.suppress(Exception):
                self.persist.save(self.label, self.epoch, record, self.seq)
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

    def __init__(self, persist: FeedStore | None = None, *, backfill: bool = True) -> None:
        self.feeds: dict[str, AccountFeed] = {}
        self.persist = persist
        # Read once at registration: loading the embedded runtime later clears
        # TELEGRAM_* variables from os.environ.
        self.backfill = backfill

    def _attach(self, label: str, client: Any) -> AccountFeed:
        from telethon import events

        feed = AccountFeed(label, self.persist)

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
        if feed.window is not None and self.backfill:
            try:
                count, complete = await asyncio.wait_for(backfill(feed, client), BACKFILL_SECONDS)
            except Exception:  # noqa: BLE001 - backfill is best effort; the window stays reported
                count, complete = 0, False
            if feed.downtime is not None:
                feed.downtime.update({"backfilled_new": count, "backfill_complete": complete})
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
        # The whole ring was examined: nothing newer than feed.seq can be pending. Without
        # this, an empty ring (events aged out across a restart) left ``last`` behind
        # ``feed.seq`` and the wait loop spun without ever awaiting.
        return out, max(last, feed.seq), False


BACKFILL_SECONDS = 20
BACKFILL_DIALOGS = 100
BACKFILL_PER_CHAT = 50
BACKFILL_TOTAL = 500


def _env_on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in {"0", "false", "no", "off"}


async def backfill(feed: AccountFeed, client: Any) -> tuple[int, bool]:
    """Re-read messages sent during the downtime window as ``new`` events.

    Dialogs are scanned newest first (pinned ones are always checked) and each
    chat is read backwards from the restart time to the window start. Edits and
    deletions during the downtime cannot be recovered. Bounded: 100 dialogs,
    50 messages per chat, 500 in total.
    """
    start, end = feed.window or (0.0, 0.0)
    found: list[Any] = []
    complete = True
    async for dialog in client.iter_dialogs(limit=BACKFILL_DIALOGS):
        date = getattr(dialog, "date", None)
        stamp = date.timestamp() if isinstance(date, datetime) else 0.0
        if stamp < start:
            if getattr(dialog, "pinned", False):
                continue
            break
        per_chat = 0
        async for message in client.iter_messages(
            dialog.entity, limit=BACKFILL_PER_CHAT, offset_date=datetime.fromtimestamp(end, UTC)
        ):
            when = getattr(message, "date", None)
            if not isinstance(when, datetime) or when.timestamp() < start:
                break
            found.append(message)
            per_chat += 1
        if per_chat >= BACKFILL_PER_CHAT or len(found) >= BACKFILL_TOTAL:
            complete = False
        if len(found) >= BACKFILL_TOTAL:
            break
    found.sort(key=lambda m: (m.date, getattr(m, "id", 0)))
    for message in found[:BACKFILL_TOTAL]:
        feed.push({**_message_record("new", message), "backfilled": True})
    return min(len(found), BACKFILL_TOTAL), complete


def parse_cursor(cursor: Any) -> tuple[str, str, int] | None:
    if cursor is None or cursor == "":
        return None
    match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValueError("cursor must be a next_cursor value returned by telegram_poll_events")
    return match.group(1), match.group(2), int(match.group(3))


def register_feed(server: Any, runtime: Any, surface: Any) -> None:
    persist = FeedStore(runtime.store) if _env_on("TELEGRAM_FEED_PERSIST") else None
    registry = EventFeed(persist, backfill=_env_on("TELEGRAM_FEED_BACKFILL"))
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
        if any(not entry.lstrip("-").isdigit() for entry in surface.chats.denied):
            # Feed events carry numeric chat ids only: a denylist entry given as @username
            # could not be matched and its chat would leak into the feed. Fail closed.
            raise policy_error("chat_access_denied")
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
        feed.touch()
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
        gap_window = None
        if not gap and feed.downtime is not None and after <= feed.boot_seq:
            # Cursor from before this process: stored events still flow, only the
            # downtime itself is unknown - re-read just that window.
            gap, gap_window = True, feed.downtime
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
                **({"gap_window": gap_window} if gap_window else {}),
                "account": label,
            }
        )

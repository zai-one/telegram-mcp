"""SQLite admission, metadata audit and durable Telegram outbox.

Unknown sends remain pending across restart. Changing a key cannot resubmit the
same pending intent. Operators reconcile externally; this server never guesses.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime

from zai_telegram.config import ServiceConfig
from zai_telegram.transport import ProviderAdmissionDenied, ProviderError, canonical_json


class StateStore:
    def __init__(self, config: ServiceConfig):
        self.config, self.path, self.account = config, config.state_path, config.account_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ValueError("state must be a regular private database")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        if os.name != "nt" and self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("state database must be owner-private")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_outbox(
                    account TEXT NOT NULL, actor TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    tool TEXT NOT NULL, account_label TEXT NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, batch_id TEXT, item_id TEXT,
                    recipient_ref TEXT, chat_id TEXT, error_code TEXT,
                    created_at TEXT NOT NULL, settled_at TEXT,
                    PRIMARY KEY(account, actor, idempotency_key));
                CREATE INDEX IF NOT EXISTS telegram_pending ON telegram_outbox(
                    account, actor, account_label, tool, request_hash, status);
                CREATE TABLE IF NOT EXISTS telegram_audit(
                    execution_id TEXT PRIMARY KEY, account TEXT NOT NULL, actor TEXT NOT NULL,
                    tool TEXT NOT NULL, args_hash TEXT NOT NULL, started REAL NOT NULL,
                    outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telegram_attempts(
                    execution_id TEXT NOT NULL, account TEXT NOT NULL, actor TEXT NOT NULL,
                    account_label TEXT NOT NULL, kind TEXT NOT NULL, started REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS telegram_rate_window ON telegram_attempts(account, started);
                CREATE TABLE IF NOT EXISTS telegram_leases(
                    execution_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                    account_label TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS telegram_cooldowns(
                    account TEXT NOT NULL, account_label TEXT NOT NULL, until_time REAL NOT NULL,
                    PRIMARY KEY(account, account_label));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    async def provider_binding(self, actor, provider):
        if provider != "telegram":
            raise PermissionError("unknown provider")
        return self.config.bindings.get(str(actor))

    @staticmethod
    def _record(row):
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    async def telegram_write_reserve(
        self,
        actor,
        idempotency_key,
        *,
        tool,
        account_label,
        request_hash,
        batch_id=None,
        item_id=None,
        recipient_ref=None,
        chat_id=None,
    ):
        actor = str(actor)
        if self.config.bindings.get(actor) != account_label:
            raise PermissionError("account binding mismatch")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValueError("bounded idempotency key required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM telegram_outbox WHERE account=? AND actor=? AND idempotency_key=?",
                (self.account, actor, idempotency_key),
            ).fetchone()
            if existing:
                if existing["account_label"] != account_label or existing["tool"] != tool:
                    raise ProviderError("idempotency key belongs to a different account or tool")
                return False, self._record(existing)
            pending = db.execute(
                "SELECT 1 FROM telegram_outbox WHERE account=? AND actor=? AND account_label=? "
                "AND tool=? AND request_hash=? AND status='pending'",
                (self.account, actor, account_label, tool, request_hash),
            ).fetchone()
            if pending:
                raise ProviderError("same write is pending reconciliation; do not resubmit")
            db.execute(
                "INSERT INTO telegram_outbox VALUES(?,?,?,?,?,?,'pending',NULL,?,?,?,?,NULL,?,NULL)",
                (
                    self.account,
                    actor,
                    idempotency_key,
                    tool,
                    account_label,
                    request_hash,
                    batch_id,
                    item_id,
                    recipient_ref,
                    chat_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return True, None

    async def telegram_write_settle(self, actor, idempotency_key, *, status, result, error_code=None):
        if status not in {"pending", "sent", "failed"}:
            raise ValueError("invalid ledger status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE telegram_outbox SET status=?,result=?,error_code=?,settled_at=? "
                "WHERE account=? AND actor=? AND idempotency_key=? AND status='pending'",
                (
                    status,
                    canonical_json(result) if result is not None else None,
                    error_code,
                    datetime.now(UTC).isoformat(),
                    self.account,
                    str(actor),
                    idempotency_key,
                ),
            ).rowcount
            if changed != 1:
                raise ProviderError("ledger reservation missing or already settled")

    async def telegram_write_list(
        self, actor, *, batch_id=None, idempotency_keys=None, status=None, limit=50
    ):
        clauses, values = ["account=?", "actor=?"], [self.account, str(actor)]
        for name, value in (("batch_id", batch_id), ("status", status)):
            if value is not None:
                clauses.append(name + "=?")
                values.append(value)
        if idempotency_keys is not None:
            if not idempotency_keys:
                return []
            clauses.append("idempotency_key IN (" + ",".join("?" for _ in idempotency_keys) + ")")
            values.extend(idempotency_keys)
        with self.connect() as db:
            return [
                self._record(row)
                for row in db.execute(
                    "SELECT * FROM telegram_outbox WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at DESC LIMIT ?",
                    (*values, min(limit, 100)),
                )
            ]

    async def consume_provider_request(self, actor, provider, limit):
        if provider != "telegram:high":
            raise ValueError("unknown quota")
        label = self.config.bindings.get(str(actor))
        if not label:
            raise PermissionError("no account binding")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            count = db.execute(
                "SELECT COUNT(*) FROM telegram_attempts WHERE account=? AND account_label=? "
                "AND kind='high' AND started>?",
                (self.account, label, now - 60),
            ).fetchone()[0]
            if count >= min(limit, self.config.telegram_high_risk_rate_limit_per_minute):
                return False
            db.execute(
                "INSERT INTO telegram_attempts VALUES('',?,?,?,'high',?)",
                (self.account, str(actor), label, now),
            )
            return True

    def begin(self, execution_id, actor, tool, digest):
        with self.connect() as db:
            db.execute(
                "INSERT INTO telegram_audit VALUES(?,?,?,?,?,?,'started')",
                (execution_id, self.account, actor, tool, digest, time.time()),
            )

    def admit(self, execution_id, actor, *, write):
        label = self.config.bindings.get(actor)
        if not label:
            raise PermissionError("no account binding")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("DELETE FROM telegram_leases WHERE expires<=?", (now,))
            cooldown = db.execute(
                "SELECT until_time FROM telegram_cooldowns WHERE account=? AND account_label=?",
                (self.account, label),
            ).fetchone()
            if cooldown and cooldown[0] > now:
                raise ProviderAdmissionDenied("provider cooldown", retry_after_seconds=60)
            active = db.execute(
                "SELECT COUNT(*) FROM telegram_leases WHERE account=? AND account_label=? "
                "AND execution_id!=?",
                (self.account, label, execution_id),
            ).fetchone()[0]
            total, writes = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(kind='write'),0) FROM telegram_attempts "
                "WHERE account=? AND account_label=? AND kind!='high' AND started>?",
                (self.account, label, now - 60),
            ).fetchone()
            if (
                active >= self.config.max_concurrency
                or total >= self.config.rate_limit
                or (write and writes >= self.config.write_rate_limit)
            ):
                raise ProviderAdmissionDenied("provider quota reached", retry_after_seconds=60)
            db.execute(
                "INSERT INTO telegram_attempts VALUES(?,?,?,?,?,?)",
                (execution_id, self.account, actor, label, "write" if write else "read", now),
            )
            # A request has a 125s deadline, and each embedded call a 120s timeout.
            db.execute(
                "INSERT OR REPLACE INTO telegram_leases VALUES(?,?,?,?)",
                (execution_id, self.account, label, now + 140),
            )

    def cooldown(self, actor, seconds):
        label = self.config.bindings.get(actor)
        with self.connect() as db:
            db.execute(
                "INSERT INTO telegram_cooldowns VALUES(?,?,?) ON CONFLICT(account,account_label) "
                "DO UPDATE SET until_time=MAX(until_time,excluded.until_time)",
                (self.account, label, time.time() + max(1, min(seconds, 3600))),
            )

    def finish(self, execution_id, outcome):
        with self.connect() as db:
            db.execute("UPDATE telegram_audit SET outcome=? WHERE execution_id=?", (outcome, execution_id))
            db.execute("DELETE FROM telegram_leases WHERE execution_id=?", (execution_id,))

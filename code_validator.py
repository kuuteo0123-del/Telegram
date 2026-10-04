"""Durable inbox v2 with batched SQLite writes."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from logger_setup import logger


class DurableInboxV2:
    def __init__(
        self,
        db_path: str = "data/telegram_inbox.db",
        lease_seconds: int = 300,
        max_attempts: int = 5,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 120.0,
        batch_size: int = 1000,
    ):
        self.db_path = str(db_path)
        self.lease_seconds = max(30, int(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = max(0.1, float(retry_base_delay))
        self.retry_max_delay = max(self.retry_base_delay, float(retry_max_delay))
        self.batch_size = max(10, int(batch_size))

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()
        self._pending_enqueues: list[tuple] = []
        self._flush_callback: Callable[[], None] | None = None

    def _configure(self):
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=30000",
            "PRAGMA cache_size=-32000",
            "PRAGMA temp_store=MEMORY",
        ):
            try:
                self._conn.execute(pragma)
            except sqlite3.DatabaseError:
                pass
        self._conn.commit()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_date TEXT,
                    edited INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    has_media INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    remaining_items INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    locked_at TEXT,
                    claim_token TEXT,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    UNIQUE(chat_id, message_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_ready ON telegram_inbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_inbox_pending_due ON telegram_inbox(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_message ON telegram_inbox(chat_id, message_id);
                CREATE TABLE IF NOT EXISTS telegram_inbox_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inbox_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    target_url TEXT NOT NULL DEFAULT '',
                    fanout_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    UNIQUE(inbox_id, domain, code, fanout_index),
                    FOREIGN KEY(inbox_id) REFERENCES telegram_inbox(id)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_items_due ON telegram_inbox_items(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_items_row ON telegram_inbox_items(inbox_id, status);
                """
            )
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def content_hash(text: str = "", has_media: bool = False, spoiler_signature: str = "") -> str:
        payload_text = f"{text or ''}\x1f{int(bool(has_media))}"
        if spoiler_signature:
            payload_text = f"{payload_text}\x1fspoiler:{spoiler_signature}"
        return hashlib.sha256(payload_text.encode("utf-8", "ignore")).hexdigest()[:32]

    def set_flush_callback(self, callback: Callable[[], None]) -> None:
        self._flush_callback = callback

    def enqueue(self, chat_id: int, message_id: int, message_date: Any, edited: bool, text: str, has_media: bool, content_hash: str | None = None) -> int | None:
        h = content_hash or self.content_hash(text, has_media)
        d = message_date.isoformat() if hasattr(message_date, "isoformat") else str(message_date or "")
        now = self._now()
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM telegram_inbox WHERE chat_id=? AND message_id=? AND content_hash=?",
                (int(chat_id), int(message_id), h),
            ).fetchone():
                return 0
            self._pending_enqueues.append((int(chat_id), int(message_id), d, int(bool(edited)), h, text or "", int(bool(has_media)), now, now))
            if len(self._pending_enqueues) >= self.batch_size:
                self._flush_enqueue_locked()
                if self._flush_callback:
                    self._flush_callback()
            return 1

    def _flush_enqueue_locked(self) -> int:
        if not self._pending_enqueues:
            return 0
        try:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO telegram_inbox
                (chat_id, message_id, message_date, edited, content_hash, text, has_media, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                self._pending_enqueues,
            )
            self._conn.commit()
            count = len(self._pending_enqueues)
            self._pending_enqueues.clear()
            return count
        except Exception as exc:
            self._conn.rollback()
            logger.error("❌ flush enqueue failed: %s", exc)
            self._pending_enqueues.clear()
            return 0

    def flush_enqueue_batch(self) -> int:
        with self._lock:
            return self._flush_enqueue_locked()

    def pending_ids(self, limit: int = 500) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM telegram_inbox WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [int(r[0]) for r in rows]

    def claim(self, row_id: int) -> dict[str, Any] | None:
        now = self._now()
        token = uuid.uuid4().hex
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='processing', attempts=attempts+1, locked_at=?, claim_token=?, updated_at=?
                WHERE id=? AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
                """,
                (now, token, now, int(row_id)),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            row = self._conn.execute("SELECT * FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
            self._conn.commit()
            return dict(row) if row else None

    def create_work_items(self, row_id: int, items: list[dict[str, Any]], claim_token: str | None = None) -> list[int]:
        with self._lock:
            row_id = int(row_id)
            params = []
            for item in items:
                params.append((row_id, str(item.get("code", "")), str(item.get("domain", "")), str(item.get("target_url", "")), int(item.get("fanout_index", 0)), self._now()))
            if params:
                self._conn.executemany(
                    """
                    INSERT OR IGNORE INTO telegram_inbox_items
                    (inbox_id, code, domain, target_url, fanout_index, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    params,
                )
            sql = """UPDATE telegram_inbox SET remaining_items=(SELECT COUNT(*) FROM telegram_inbox_items WHERE inbox_id=? AND status IN ('pending','processing')), status='processing', next_attempt_at=NULL, updated_at=? WHERE id=? AND status='processing'"""
            args = [row_id, self._now(), row_id]
            if claim_token:
                sql += " AND claim_token=?"
                args.append(str(claim_token))
            self._conn.execute(sql, args)
            ids = self._conn.execute("SELECT id FROM telegram_inbox_items WHERE inbox_id=? AND status='pending' ORDER BY id", (row_id,)).fetchall()
            self._conn.commit()
            return [int(r[0]) for r in ids]

    def claim_work_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                "UPDATE telegram_inbox_items SET status='processing', attempts=attempts+1, updated_at=? WHERE id=? AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))",
                (now, int(item_id)),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            row = self._conn.execute("SELECT * FROM telegram_inbox_items WHERE id=?", (int(item_id),)).fetchone()
            self._conn.commit()
            return dict(row) if row else None

    def complete_work_item(self, item_id: int) -> bool:
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                "UPDATE telegram_inbox_items SET status='completed', completed_at=?, updated_at=? WHERE id=? AND status='processing'",
                (now, now, int(item_id)),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return False
            self._conn.execute(
                """UPDATE telegram_inbox SET remaining_items=(SELECT COUNT(*) FROM telegram_inbox_items WHERE inbox_id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?) AND status IN ('pending','processing')), updated_at=? WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)""",
                (int(item_id), now, int(item_id)),
            )
            self._conn.execute(
                """UPDATE telegram_inbox SET status='completed', completed_at=?, locked_at=NULL, updated_at=? WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?) AND status IN ('pending','processing') AND remaining_items=0""",
                (now, now, int(item_id)),
            )
            self._conn.commit()
            return True

    def retry_work_item(self, item_id: int, error: str, delay_seconds: float = 10) -> str:
        with self._lock:
            row = self._conn.execute("SELECT attempts, status FROM telegram_inbox_items WHERE id=?", (int(item_id),)).fetchone()
            if not row or row[1] in {"completed", "failed"}:
                return str(row[1]) if row else "missing"
            attempts = int(row[0] or 0)
            now = self._now()
            if attempts >= self.max_attempts:
                self._conn.execute("UPDATE telegram_inbox_items SET status='failed', last_error=?, updated_at=? WHERE id=?", (f"{error} (max_attempts={self.max_attempts})"[:500], now, int(item_id)))
                result = "failed"
            else:
                delay = min(self.retry_max_delay, max(0.1, float(delay_seconds)) * (2 ** max(0, attempts - 1)))
                self._conn.execute(
                    "UPDATE telegram_inbox_items SET status='pending', last_error=?, next_attempt_at=datetime('now', ?), updated_at=? WHERE id=? AND status='processing'",
                    (error[:500], f"+{int(round(delay))} seconds", now, int(item_id)),
                )
                result = "retried"
            self._conn.execute(
                """UPDATE telegram_inbox SET remaining_items=(SELECT COUNT(*) FROM telegram_inbox_items WHERE inbox_id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=? ) AND status IN ('pending','processing')), updated_at=? WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)""",
                (int(item_id), now, int(item_id)),
            )
            self._conn.commit()
            return result

    def due_work_items(self, limit: int = 250) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.*, r.claim_token FROM telegram_inbox_items i JOIN telegram_inbox r ON r.id=i.inbox_id WHERE i.status='pending' AND r.status IN ('pending','processing') AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= datetime('now')) ORDER BY i.id LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def retry_or_fail(self, row_id: int, error: str, base_delay: float | None = None, claim_token: str | None = None) -> str:
        row_id = int(row_id)
        delay = self.retry_base_delay if base_delay is None else max(0.1, float(base_delay))
        with self._lock:
            row = self._conn.execute("SELECT attempts, status, claim_token FROM telegram_inbox WHERE id=?", (row_id,)).fetchone()
            if not row:
                return "missing"
            attempts = int(row[0] or 0)
            status = str(row[1] or "")
            current_token = str(row[2] or "")
            if claim_token and current_token != str(claim_token):
                return "stale_claim"
            if status in {"completed", "ignored", "failed"}:
                return status
            now = self._now()
            if attempts >= self.max_attempts:
                sql = "UPDATE telegram_inbox SET status='failed', last_error=?, locked_at=NULL, updated_at=? WHERE id=? AND status IN ('pending','processing')"
                params = [f"{error} (max_attempts={self.max_attempts})"[:500], now, row_id]
                if claim_token:
                    sql += " AND claim_token=?"
                    params.append(str(claim_token))
                self._conn.execute(sql, params)
                self._conn.commit()
                return "failed"
            backoff = min(self.retry_max_delay, delay * (2 ** max(0, attempts - 1)))
            sql = "UPDATE telegram_inbox SET status='pending', remaining_items=0, last_error=?, locked_at=NULL, next_attempt_at=datetime('now', ?), updated_at=? WHERE id=? AND status IN ('pending','processing')"
            params = [error[:500], f"+{int(round(backoff))} seconds", now, row_id]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(str(claim_token))
            self._conn.execute(sql, params)
            self._conn.commit()
            return "retried"

    def mark_ignored(self, row_id: int, reason: str = "no_code", claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            sql = "UPDATE telegram_inbox SET status='ignored', last_error=?, locked_at=NULL, updated_at=?, completed_at=? WHERE id=? AND status='processing'"
            params = [reason[:500], now, now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(claim_token)
            self._conn.execute(sql, params)
            self._conn.commit()

    def mark_failed(self, row_id: int, error: str, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            sql = "UPDATE telegram_inbox SET status='failed', last_error=?, locked_at=NULL, updated_at=? WHERE id=? AND status='processing'"
            params = [error[:500], now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(claim_token)
            self._conn.execute(sql, params)
            self._conn.commit()

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
            return dict(row) if row else None

    def purge_completed(self, keep_days: int = 7, batch_size: int = 500) -> int:
        cutoff = f"-{max(1, int(keep_days))} days"
        batch = max(50, int(batch_size))
        total = 0
        while True:
            with self._lock:
                ids = [
                    int(r[0]) for r in self._conn.execute(
                        "SELECT id FROM telegram_inbox WHERE status IN ('completed','ignored') AND completed_at < datetime('now', ?) LIMIT ?",
                        (cutoff, batch),
                    ).fetchall()
                ]
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM telegram_inbox_items WHERE inbox_id IN ({marks})", ids)
                cur = self._conn.execute(f"DELETE FROM telegram_inbox WHERE id IN ({marks})", ids)
                self._conn.commit()
                total += int(cur.rowcount or 0)
        return total

    def close(self):
        with self._lock:
            self._flush_enqueue_locked()
            self._conn.close()


__all__ = ["DurableInboxV2"]

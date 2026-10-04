"""Durable Telegram inbox for at-least-once browser submission.

The inbox stores only serializable Telegram metadata. Workers keep a lightweight
row_id in RAM and re-fetch the Telegram message when they claim the row.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger_setup import logger


class DurableInbox:
    def __init__(
        self,
        db_path: str = "data/telegram_inbox.db",
        lease_seconds: int = 300,
        max_attempts: int = 5,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 120.0,
    ):
        self.db_path = str(db_path)
        self.lease_seconds = max(30, int(lease_seconds))
        # ✅ FIX: chặn retry vô hạn — trước đây retry() không có giới hạn số
        # lần thử, nên 1 tin nhắn bị site trả NO_RESULT/lỗi liên tục sẽ bị
        # replay lại (fetch lại message, extract lại code, submit lại) MÃI
        # MÃI mỗi vài giây, chiếm slot xử lý và làm trễ tin nhắn mới thật sự
        # (message_queue/domain_queue bị dồn ứ bởi backlog cũ). Giờ mỗi
        # dòng bị giới hạn tối đa max_attempts lần claim; vượt quá sẽ tự
        # động mark_failed thay vì tiếp tục replay — xem retry_or_fail().
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = max(0.1, float(retry_base_delay))
        self.retry_max_delay = max(self.retry_base_delay, float(retry_max_delay))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=10000",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA cache_size=-16000",
        ):
            try:
                self._conn.execute(pragma)
            except sqlite3.DatabaseError:
                pass
        self._conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            # ✅ FIX (dedup): khoá UNIQUE trước đây là
            # (chat_id, message_id, edited, content_hash) — cột 'edited'
            # nằm TRONG khoá khiến Telegram gửi event "edited" cho ĐÚNG
            # tin nhắn cũ (vd chỉ view-count cập nhật, nội dung không đổi)
            # tạo ra một khoá KHÁC (edited 0→1) → INSERT OR IGNORE không
            # ignore được nữa mà chèn thêm 1 DÒNG MỚI cho cùng 1 tin nhắn.
            # Dòng mới này bị worker lấy ra xử lý lại từ đầu → OCR lại,
            # submit lại y hệt các mã cũ 10-15 phút sau (đã xác nhận qua
            # log: cùng message_id xuất hiện 2 lần, cách nhau ~15 phút).
            # Khoá đúng chỉ nên dựa vào NỘI DUNG thực sự của tin nhắn:
            # (chat_id, message_id, content_hash) — 'edited' không còn là
            # 1 phần của khoá, chỉ lưu để tham khảo/log.
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
                CREATE INDEX IF NOT EXISTS idx_inbox_ready
                    ON telegram_inbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_inbox_pending_due
                    ON telegram_inbox(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_message
                    ON telegram_inbox(chat_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_inbox_retention
                    ON telegram_inbox(status, completed_at);
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
                CREATE INDEX IF NOT EXISTS idx_inbox_items_due
                    ON telegram_inbox_items(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_items_status_domain_due
                    ON telegram_inbox_items(status, domain, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_items_row
                    ON telegram_inbox_items(inbox_id, status);
                """
            )
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(telegram_inbox)").fetchall()
            }
            if "next_attempt_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE telegram_inbox ADD COLUMN next_attempt_at TEXT"
                )
            if "claim_token" not in columns:
                self._conn.execute(
                    "ALTER TABLE telegram_inbox ADD COLUMN claim_token TEXT"
                )
            self._conn.commit()
            # DB có sẵn từ trước khi vá lỗi này vẫn còn schema cũ (UNIQUE
            # bao gồm 'edited') — CREATE TABLE IF NOT EXISTS ở trên không
            # đổi được bảng đã tồn tại, nên phải migrate riêng.
            self._migrate_drop_edited_from_unique()
            self._conn.commit()

    def _migrate_drop_edited_from_unique(self) -> None:
        """Nâng cấp DB cũ: bỏ cột 'edited' khỏi khoá UNIQUE để chặn dòng
        trùng khi Telegram gửi event edited cho tin nhắn không đổi nội
        dung (xem giải thích chi tiết ở _init_schema). An toàn để gọi mỗi
        lần khởi động — chỉ thực sự chạy khi phát hiện đúng schema cũ."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='telegram_inbox'"
        ).fetchone()
        if not row or not row[0]:
            return
        table_sql_compact = row[0].lower().replace(" ", "").replace("\n", "")
        if "unique(chat_id,message_id,edited,content_hash)" not in table_sql_compact:
            return  # Đã ở schema mới (hoặc bảng vừa mới tạo) — không cần migrate.

        logger.warning(
            "🔧 [Inbox] Phát hiện schema cũ (UNIQUE có 'edited') — đang migrate "
            "để chặn dòng trùng do Telegram gửi event edited (vd view-count "
            "cập nhật) cho tin nhắn không đổi nội dung..."
        )
        try:
            self._conn.execute("ALTER TABLE telegram_inbox RENAME TO telegram_inbox_old")
            self._conn.execute(
                """
                CREATE TABLE telegram_inbox (
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
                )
                """
            )

            old_cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(telegram_inbox_old)").fetchall()
            ]
            rows = self._conn.execute(f"SELECT {','.join(old_cols)} FROM telegram_inbox_old").fetchall()

            # Nhiều dòng cũ có thể trùng (chat_id, message_id, content_hash)
            # do chính bug này — gộp lại, ưu tiên giữ dòng phản ánh kết quả
            # "chín" nhất (completed > processing > failed > pending >
            # ignored), rồi tới dòng cập nhật gần nhất.
            status_rank = {"completed": 0, "processing": 1, "failed": 2, "pending": 3, "ignored": 4}
            best: dict[tuple, dict] = {}
            for r in rows:
                d = dict(zip(old_cols, r))
                key = (d["chat_id"], d["message_id"], d["content_hash"])
                cur = best.get(key)
                if cur is None:
                    best[key] = d
                    continue
                rank_new = status_rank.get(d.get("status"), 5)
                rank_old = status_rank.get(cur.get("status"), 5)
                if rank_new < rank_old or (
                    rank_new == rank_old
                    and str(d.get("updated_at") or "") > str(cur.get("updated_at") or "")
                ):
                    best[key] = d

            insert_cols = [c for c in old_cols if c != "id"]
            placeholders = ",".join("?" for _ in insert_cols)
            for d in best.values():
                self._conn.execute(
                    f"INSERT INTO telegram_inbox ({','.join(insert_cols)}) VALUES ({placeholders})",
                    [d[c] for c in insert_cols],
                )

            self._conn.execute("DROP TABLE telegram_inbox_old")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_ready ON telegram_inbox(status, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_pending_due ON telegram_inbox(status, next_attempt_at, id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_message ON telegram_inbox(chat_id, message_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_retention ON telegram_inbox(status, completed_at)"
            )
            self._conn.commit()

            kept, total = len(best), len(rows)
            logger.warning(
                "✅ [Inbox] Migration xong: giữ %s/%s dòng (đã gộp %s dòng trùng "
                "chat_id+message_id+content_hash phát sinh từ bug 'edited' cũ)",
                kept, total, total - kept,
            )
        except Exception:
            self._conn.rollback()
            logger.exception(
                "❌ [Inbox] Migration schema thất bại — DB có thể vẫn ở schema cũ. "
                "Kiểm tra thủ công file %s trước khi chạy lại bot.",
                self.db_path,
            )
            raise

    @staticmethod
    def content_hash(
        text: str = "",
        has_media: bool = False,
        spoiler_signature: str = "",
    ) -> str:
        payload_text = f"{text or ''}\x1f{int(bool(has_media))}"
        if spoiler_signature:
            payload_text = f"{payload_text}\x1fspoiler:{spoiler_signature}"
        payload = payload_text.encode("utf-8", "ignore")
        return hashlib.sha256(payload).hexdigest()[:32]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def enqueue(
        self,
        chat_id: int,
        message_id: int,
        message_date: Any,
        edited: bool,
        text: str,
        has_media: bool,
        content_hash: str | None = None,
    ) -> int | None:
        """Insert once and return a new row id.

        Duplicate Telegram deliveries return ``None``. Returning the existing
        row id is unsafe because the ingress path would enqueue a completed (or
        already-processing) row again, replaying the same message after an
        edited/view-count update or a transport redelivery.
        """
        h = content_hash or self.content_hash(text, has_media)
        date_text = message_date.isoformat() if hasattr(message_date, "isoformat") else str(message_date or "")
        now = self._now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO telegram_inbox
                    (chat_id, message_id, message_date, edited, content_hash, text, has_media,
                     status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (int(chat_id), int(message_id), date_text, int(bool(edited)), h, text or "", int(bool(has_media)), now, now),
                )
                self._conn.commit()
                # INSERT OR IGNORE may expose sqlite3's previous successful
                # lastrowid when this insert was ignored. Use rowcount as the
                # reliable inserted-vs-duplicate signal under concurrent
                # delivery. A duplicate must not return the old row id: the
                # caller would enqueue that old row and replay the message.
                if cur.rowcount == 1 and cur.lastrowid:
                    return int(cur.lastrowid)
                return 0  # trùng: không phải lỗi DB
            except Exception as exc:
                self._conn.rollback()
                logger.error("❌ [Inbox] enqueue lỗi: %s", exc)
                return None

    def reclaim_stale(self) -> int:
        with self._lock:
            stale = self._conn.execute(
                "SELECT id FROM telegram_inbox WHERE status='processing' AND locked_at < datetime('now', ?)",
                (f"-{self.lease_seconds} seconds",),
            ).fetchall()
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='pending', locked_at=NULL, updated_at=?
                WHERE status='processing'
                  AND locked_at < datetime('now', ?)
                """,
                (self._now(), f"-{self.lease_seconds} seconds"),
            )
            if stale:
                self._conn.executemany(
                    """UPDATE telegram_inbox_items SET status='pending', next_attempt_at=NULL,
                    updated_at=? WHERE inbox_id=? AND status='processing'""",
                    [(self._now(), int(row[0])) for row in stale],
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def ignore_pending_before(self, cutoff: datetime, reason: str = "startup_old_message") -> int:
        """Mark pending inbox rows older than ``cutoff`` as ignored.

        New-message-only mode must not replay rows left by a previous run.
        Dates are parsed in Python because Telegram timestamps may contain
        different ISO-8601 offsets.
        """
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        cutoff = cutoff.astimezone(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, message_date FROM telegram_inbox WHERE status='pending'"
            ).fetchall()
            old_ids = []
            for row in rows:
                raw = str(row[1] or "").strip()
                try:
                    message_date = datetime.fromisoformat(raw)
                    if message_date.tzinfo is None:
                        message_date = message_date.replace(tzinfo=timezone.utc)
                    if message_date.astimezone(timezone.utc) < cutoff:
                        old_ids.append(int(row[0]))
                except (TypeError, ValueError, OverflowError):
                    # Invalid timestamps are unsafe to replay in strict mode.
                    old_ids.append(int(row[0]))
            if old_ids:
                now = self._now()
                self._conn.executemany(
                    """
                    UPDATE telegram_inbox
                    SET status='ignored', last_error=?, locked_at=NULL,
                        updated_at=?, completed_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [(reason[:500], now, now, row_id) for row_id in old_ids],
                )
                self._conn.commit()
            return len(old_ids)

    def discard_unfinished(self, reason: str = "startup_discard_unfinished") -> int:
        """Discard all unfinished local rows before a fresh live-only run.

        This never deletes Telegram messages. It only prevents pending or
        abandoned processing rows from being replayed after a restart.
        """
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='ignored', last_error=?, locked_at=NULL,
                    updated_at=?, completed_at=?
                WHERE status IN ('pending', 'processing')
                """,
                (reason[:500], now, now),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def pending_ids(self, limit: int = 500) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id FROM telegram_inbox
                WHERE status='pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
                ORDER BY id LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [int(r[0]) for r in rows]

    def claim(self, row_id: int) -> dict[str, Any] | None:
        """Atomically claim a due row and return a fencing token."""
        now = self._now()
        claim_token = uuid.uuid4().hex
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    UPDATE telegram_inbox
                    SET status='processing', attempts=attempts+1, locked_at=?, claim_token=?, updated_at=?
                    WHERE id=? AND status='pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
                    """,
                    (now, claim_token, now, int(row_id)),
                )
                if cur.rowcount != 1:
                    self._conn.commit()
                    return None
                row = self._conn.execute("SELECT * FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
                self._conn.commit()
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise

    def set_remaining(self, row_id: int, count: int, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            status = "completed" if int(count) <= 0 else "processing"
            self._conn.execute(
                "UPDATE telegram_inbox SET remaining_items=?, status=?, next_attempt_at=NULL, updated_at=?, completed_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (max(0, int(count)), status, now, now if status == "completed" else None, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def complete_item(self, row_id: int, claim_token: str | None = None) -> bool:
        """Decrement work count; true when the durable row is fully completed."""
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE telegram_inbox SET remaining_items=MAX(remaining_items-1, 0), updated_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='completed', completed_at=?, locked_at=NULL, updated_at=?
                WHERE id=? AND status='processing' AND remaining_items=0
                """ + (" AND claim_token=?" if claim_token else ""),
                (now, now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            row = self._conn.execute("SELECT status FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
            self._conn.commit()
            return bool(row and row[0] == "completed")

    def create_work_items(
        self, row_id: int, items: list[dict[str, Any]], claim_token: str | None = None
    ) -> list[int]:
        """Persist one independent work item per domain/account fanout slot."""
        with self._lock:
            row_id = int(row_id)
            params = []
            for item in items:
                params.append((
                    row_id, str(item.get("code", "")), str(item.get("domain", "")),
                    str(item.get("target_url", "")), int(item.get("fanout_index", 0)),
                ))
            if params:
                self._conn.executemany(
                    """INSERT OR IGNORE INTO telegram_inbox_items
                    (inbox_id, code, domain, target_url, fanout_index, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    [(a, b, c, d, e, self._now()) for a, b, c, d, e in params],
                )
            sql = """UPDATE telegram_inbox SET remaining_items=(
                SELECT COUNT(*) FROM telegram_inbox_items
                WHERE inbox_id=? AND status IN ('pending','processing')),
                status='processing', next_attempt_at=NULL, updated_at=?
                WHERE id=? AND status='processing'"""
            args = [row_id, self._now(), row_id]
            if claim_token:
                sql += " AND claim_token=?"
                args.append(str(claim_token))
            self._conn.execute(sql, args)
            ids = self._conn.execute(
                "SELECT id FROM telegram_inbox_items WHERE inbox_id=? AND status='pending' ORDER BY id",
                (row_id,),
            ).fetchall()
            self._conn.commit()
            return [int(r[0]) for r in ids]

    def claim_work_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                """UPDATE telegram_inbox_items SET status='processing', attempts=attempts+1,
                updated_at=? WHERE id=? AND status='pending'
                AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))""",
                (now, int(item_id)),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            row = self._conn.execute("SELECT * FROM telegram_inbox_items WHERE id=?", (int(item_id),)).fetchone()
            self._conn.commit()
            return dict(row) if row else None

    def complete_work_item(self, item_id: int) -> bool:
        """Complete exactly one item and complete its parent only when all items finish."""
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                """UPDATE telegram_inbox_items SET status='completed', completed_at=?,
                updated_at=? WHERE id=? AND status='processing'""",
                (now, now, int(item_id)),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return False
            self._conn.execute(
                """UPDATE telegram_inbox SET remaining_items=(SELECT COUNT(*) FROM telegram_inbox_items
                WHERE inbox_id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=? )
                AND status IN ('pending','processing')), updated_at=?
                WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)""",
                (int(item_id), now, int(item_id)),
            )
            self._conn.execute(
                """UPDATE telegram_inbox SET status='completed', completed_at=?, locked_at=NULL,
                updated_at=? WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=? )
                AND status IN ('pending','processing') AND remaining_items=0""",
                (now, now, int(item_id)),
            )
            self._conn.commit()
            return True

    def retry_work_item(self, item_id: int, error: str, delay_seconds: float = 10) -> str:
        """Retry only one domain/account item; parent row stays processing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, status FROM telegram_inbox_items WHERE id=?", (int(item_id),)
            ).fetchone()
            if not row or row[1] in {"completed", "failed"}:
                return str(row[1]) if row else "missing"
            attempts = int(row[0] or 0)
            now = self._now()
            if attempts >= self.max_attempts:
                self._conn.execute(
                    "UPDATE telegram_inbox_items SET status='failed', last_error=?, updated_at=? WHERE id=?",
                    (f"{error} (max_attempts={self.max_attempts})"[:500], now, int(item_id)),
                )
                result = "failed"
            else:
                delay = min(self.retry_max_delay, max(0.1, float(delay_seconds)) * (2 ** max(0, attempts - 1)))
                self._conn.execute(
                    """UPDATE telegram_inbox_items SET status='pending', last_error=?,
                    next_attempt_at=datetime('now', ?), updated_at=? WHERE id=? AND status='processing'""",
                    (error[:500], f"+{int(round(delay))} seconds", now, int(item_id)),
                )
                result = "retried"
            self._conn.execute(
                """UPDATE telegram_inbox SET remaining_items=(SELECT COUNT(*) FROM telegram_inbox_items
                WHERE inbox_id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)
                AND status IN ('pending','processing')), updated_at=?
                WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)""",
                (int(item_id), now, int(item_id)),
            )
            if result == "failed":
                self._conn.execute(
                    """UPDATE telegram_inbox SET status='failed', locked_at=NULL, updated_at=?
                    WHERE id=(SELECT inbox_id FROM telegram_inbox_items WHERE id=?)
                    AND remaining_items=0 AND status IN ('pending','processing')""",
                    (now, int(item_id)),
                )
            self._conn.commit()
            return result

    def due_work_items(self, limit: int = 250) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.*, r.claim_token FROM telegram_inbox_items i
                JOIN telegram_inbox r ON r.id=i.inbox_id
                WHERE i.status='pending' AND r.status IN ('pending','processing')
                AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= datetime('now'))
                ORDER BY i.id LIMIT ?""", (max(1, int(limit)),)
            ).fetchall()
            return [dict(r) for r in rows]

    def has_active_work_items(self, row_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM telegram_inbox_items WHERE inbox_id=? AND status IN ('pending','processing') LIMIT 1",
                (int(row_id),),
            ).fetchone()
            return bool(row)

    def mark_ignored(self, row_id: int, reason: str = "no_code", claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE telegram_inbox SET status='ignored', last_error=?, locked_at=NULL, updated_at=?, completed_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (reason[:500], now, now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def mark_ignored_if_empty(
        self,
        row_id: int,
        reason: str = "no_code_or_not_routed",
        claim_token: str | None = None,
    ) -> bool:
        """Atomically ignore a processing row only when no fanout remains."""
        with self._lock:
            now = self._now()
            sql = (
                "UPDATE telegram_inbox SET status='ignored', last_error=?, "
                "locked_at=NULL, updated_at=?, completed_at=? "
                "WHERE id=? AND status='processing' AND remaining_items=0"
            )
            params = [reason[:500], now, now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(claim_token)
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return bool(cur.rowcount)

    def mark_failed(self, row_id: int, error: str, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE telegram_inbox SET status='failed', last_error=?, locked_at=NULL, updated_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (error[:500], now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def retry(self, row_id: int, error: str, delay_seconds: int = 2, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            delay = max(0, int(delay_seconds))
            self._conn.execute(
                "UPDATE telegram_inbox "
                "SET status='pending', remaining_items=0, last_error=?, "
                "locked_at=NULL, next_attempt_at=datetime('now', ?), updated_at=? "
                "WHERE id=? AND status='processing'"
                + (" AND claim_token=?" if claim_token else ""),
                (error[:500], f"+{delay} seconds", now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def retry_or_fail(
        self,
        row_id: int,
        error: str,
        base_delay: float | None = None,
        claim_token: str | None = None,
    ) -> str:
        """Entry point BẮT BUỘC cho mọi retry từ bên ngoài (thay cho gọi
        thẳng retry()) — tự đọc số lần 'attempts' hiện tại của dòng, và:
          - Nếu đã đạt/vượt max_attempts → mark_failed() NGAY, KHÔNG replay
            thêm nữa (chặn retry-storm vô hạn — xem __init__ để biết lý do).
          - Nếu còn hạn mức → retry() với delay tăng dần theo cấp số nhân
            (exponential backoff), giới hạn ở retry_max_delay, tránh dội
            liên tục vào 1 site đang lỗi tạm thời.

        Trả về "retried" hoặc "failed" để caller log/theo dõi nếu cần.
        """
        delay = self.retry_base_delay if base_delay is None else max(0.1, float(base_delay))
        row_id = int(row_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, status, claim_token FROM telegram_inbox WHERE id=?",
                (row_id,),
            ).fetchone()
            if not row:
                return "missing"

            attempts = int(row[0] or 0)
            status = str(row[1] or "")
            current_token = str(row[2] or "")
            if claim_token and current_token != str(claim_token):
                return "stale_claim"
            # A late failure callback must not move an already completed or
            # intentionally ignored row back into the processing pipeline.
            if status in {"completed", "ignored", "failed"}:
                return status

            now = self._now()
            if attempts >= self.max_attempts:
                sql = """
                    UPDATE telegram_inbox
                    SET status='failed', last_error=?, locked_at=NULL,
                        updated_at=?
                    WHERE id=? AND status IN ('pending', 'processing')
                """
                params = [f"{error} (đã vượt max_attempts={self.max_attempts}, dừng retry)"[:500], now, row_id]
                if claim_token:
                    sql += " AND claim_token=?"
                    params.append(str(claim_token))
                self._conn.execute(sql, params)
                self._conn.commit()
                logger.warning(
                    "🛑 [Inbox] row=%s vượt quá %s lần thử — đánh dấu 'failed', "
                    "KHÔNG replay lại tin nhắn nữa. Lỗi gần nhất: %s",
                    row_id, self.max_attempts, error,
                )
                return "failed"

            backoff = min(self.retry_max_delay, delay * (2 ** max(0, attempts - 1)))
            sql = """
                UPDATE telegram_inbox
                SET status='pending', remaining_items=0, last_error=?,
                    locked_at=NULL, next_attempt_at=datetime('now', ?), updated_at=?
                WHERE id=? AND status IN ('pending', 'processing')
            """
            params = [error[:500], f"+{int(round(backoff))} seconds", now, row_id]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(str(claim_token))
            self._conn.execute(sql, params)
            self._conn.commit()
            return "retried"

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
            return dict(row) if row else None

    def purge_completed(self, keep_days: int = 7, batch_size: int = 500) -> int:
        """Xóa row cha completed/ignored quá hạn cùng TOÀN BỘ item con.

        ✅ FIX: bản cũ chỉ xóa ``telegram_inbox`` trong khi ``PRAGMA
        foreign_keys`` đang tắt, nên ``telegram_inbox_items`` bị bỏ mồ côi và
        phình vô hạn. Giờ xóa item con trước, rồi mới xóa row cha, trong cùng
        một transaction. Xử lý theo lô và NHẢ ``_lock`` giữa các lô: ingress
        (``enqueue``) dùng chung lock này nên không được bị chặn lâu.

        Trả về số row cha đã xóa. Item mồ côi sót lại từ bản cũ cũng được dọn.
        """
        cutoff = f"-{max(1, int(keep_days))} days"
        batch = max(50, int(batch_size))
        total = 0
        while True:
            with self._lock:
                ids = [
                    int(r[0])
                    for r in self._conn.execute(
                        "SELECT id FROM telegram_inbox "
                        "WHERE status IN ('completed','ignored') "
                        "AND completed_at < datetime('now', ?) LIMIT ?",
                        (cutoff, batch),
                    ).fetchall()
                ]
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                try:
                    self._conn.execute(
                        f"DELETE FROM telegram_inbox_items WHERE inbox_id IN ({marks})", ids
                    )
                    cur = self._conn.execute(
                        f"DELETE FROM telegram_inbox WHERE id IN ({marks})", ids
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                total += int(cur.rowcount or 0)
            # Lock đã nhả ở đây → enqueue/claim có cơ hội chen vào giữa các lô.

        # Dọn item mồ côi do bản purge cũ để lại (row cha không còn tồn tại).
        while True:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM telegram_inbox_items WHERE id IN ("
                    "SELECT i.id FROM telegram_inbox_items i "
                    "LEFT JOIN telegram_inbox r ON r.id = i.inbox_id "
                    "WHERE r.id IS NULL LIMIT ?)",
                    (batch,),
                )
                self._conn.commit()
                removed = int(cur.rowcount or 0)
            if removed < batch:
                break
        return total

    def maintenance(self) -> dict[str, Any]:
        """Checkpoint WAL + VACUUM để file DB thực sự thu nhỏ sau purge.

        Chạy trên connection RIÊNG và KHÔNG giữ ``self._lock``: VACUUM có thể
        mất vài trăm ms; nếu giữ lock chung thì ``enqueue`` tin Telegram mới
        sẽ bị chặn đúng chừng đó thời gian. Connection chính chỉ phải chờ
        ``busy_timeout`` ở mức DB nếu trùng đúng lúc ghi — chấp nhận được vì
        hàm này chỉ chạy định kỳ ngoài giờ cao điểm.
        """
        import os

        before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            # Ở chế độ WAL, VACUUM ghi kết quả vào file -wal; file .db chỉ co lại
            # sau khi checkpoint. Phải checkpoint LẦN NỮA sau VACUUM.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
        after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {"before_bytes": before, "after_bytes": after}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["DurableInbox"]

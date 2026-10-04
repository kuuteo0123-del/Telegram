"""SQLite persistence for account and submission statistics."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from logger_setup import logger


class CodeDatabase:
    def __init__(self, db_path: str = "data/code_history.db", stats_batch_size: int = 50):
        self.db_path = db_path
        self.stats_batch_size = max(1, int(stats_batch_size))
        self._lock = threading.RLock()
        self._pending_account_stats: dict[str, dict] = {}
        self._pending_website_stats: dict[str, dict] = {}
        self._pending_stats_events = 0

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._init_tables()

    def _configure(self) -> None:
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=10000",
            "PRAGMA cache_size=-32000",
            "PRAGMA temp_store=MEMORY",
        ):
            try:
                self.conn.execute(pragma)
            except Exception:
                pass
        self.conn.commit()

    def _init_tables(self) -> None:
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS code_submission (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    account TEXT NOT NULL,
                    website TEXT NOT NULL,
                    status TEXT,
                    result TEXT,
                    submitted_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, account)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS used_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    code TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(domain, code)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submission_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    account TEXT NOT NULL,
                    website TEXT NOT NULL,
                    status TEXT,
                    result TEXT,
                    attempt INTEGER,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL UNIQUE,
                    total_submitted INTEGER DEFAULT 0,
                    total_success INTEGER DEFAULT 0,
                    total_failed INTEGER DEFAULT 0,
                    last_submit TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS website_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    website TEXT NOT NULL UNIQUE,
                    total_submitted INTEGER DEFAULT 0,
                    total_success INTEGER DEFAULT 0,
                    total_failed INTEGER DEFAULT 0,
                    last_submit TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON code_submission(code)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_account ON submission_log(account)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_website ON submission_log(website)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_submitted_at ON submission_log(submitted_at)")
            self.conn.commit()

    def _queue_stats_locked(self, account: str, website: str, status: str, now: datetime) -> None:
        account_row = self._pending_account_stats.setdefault(account, {"total": 0, "success": 0, "failed": 0, "last_submit": now})
        website_row = self._pending_website_stats.setdefault(website, {"total": 0, "success": 0, "failed": 0, "last_submit": now})
        for row in (account_row, website_row):
            row["total"] += 1
            row["last_submit"] = now
            if status == "SUCCESS":
                row["success"] += 1
            elif status == "FAILED":
                row["failed"] += 1
        self._pending_stats_events += 1

    def _flush_stats_locked(self) -> int:
        if not self._pending_stats_events:
            return 0
        account_rows = list(self._pending_account_stats.items())
        website_rows = list(self._pending_website_stats.items())
        try:
            self.conn.executemany(
                """
                INSERT INTO account_stats (account, total_submitted, total_success, total_failed, last_submit)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    total_submitted=account_stats.total_submitted + excluded.total_submitted,
                    total_success=account_stats.total_success + excluded.total_success,
                    total_failed=account_stats.total_failed + excluded.total_failed,
                    last_submit=excluded.last_submit
                """,
                [(key, row["total"], row["success"], row["failed"], row["last_submit"]) for key, row in account_rows],
            )
            self.conn.executemany(
                """
                INSERT INTO website_stats (website, total_submitted, total_success, total_failed, last_submit)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(website) DO UPDATE SET
                    total_submitted=website_stats.total_submitted + excluded.total_submitted,
                    total_success=website_stats.total_success + excluded.total_success,
                    total_failed=website_stats.total_failed + excluded.total_failed,
                    last_submit=excluded.last_submit
                """,
                [(key, row["total"], row["success"], row["failed"], row["last_submit"]) for key, row in website_rows],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._pending_account_stats.clear()
        self._pending_website_stats.clear()
        self._pending_stats_events = 0
        return self._pending_stats_events

    def flush_stats(self) -> int:
        with self._lock:
            try:
                return self._flush_stats_locked()
            except Exception as exc:
                logger.error("❌ flush_stats error: %s", exc)
                return 0

    def record_submission(self, code: str, account: str, website: str, status: str, result: str | None = None, attempt: int = 1) -> None:
        with self._lock:
            try:
                now = datetime.now()
                self.conn.execute(
                    "INSERT INTO submission_log (code, account, website, status, result, attempt) VALUES (?, ?, ?, ?, ?, ?)",
                    (code, account, website, status, result, attempt),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO code_submission (code, account, website, status, result, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (code, account, website, status, result, now),
                )
                self.conn.commit()
                self._queue_stats_locked(account, website, status, now)
                if self._pending_stats_events >= self.stats_batch_size:
                    self._flush_stats_locked()
            except Exception as exc:
                logger.error("❌ record_submission error: %s", exc)
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    def is_code_used(self, domain: str, code: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM used_codes WHERE domain=? AND code=?", (domain, code.upper())).fetchone()
            return row is not None

    def unused_codes(self, domain: str, codes) -> set[str]:
        normalized = list(dict.fromkeys(str(code or "").strip().upper() for code in (codes or []) if str(code or "").strip()))
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self.conn.execute(f"SELECT code FROM used_codes WHERE domain=? AND code IN ({placeholders})", (domain, *normalized)).fetchall()
            used = {str(row[0]).upper() for row in rows}
            return {code for code in normalized if code not in used}

    def mark_code_used(self, domain: str, code: str) -> bool:
        with self._lock:
            try:
                self.conn.execute("INSERT INTO used_codes (domain, code) VALUES (?, ?)", (domain, code.upper()))
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                self.conn.rollback()
                return False
            except Exception as exc:
                logger.error("❌ mark_code_used error: %s", exc)
                self.conn.rollback()
                return False

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_stats_locked()
            except Exception as exc:
                logger.error("❌ close flush stats: %s", exc)
            self.conn.close()


def init_database(db_path: str = "data/code_history.db") -> CodeDatabase:
    return CodeDatabase(db_path)


def get_database() -> CodeDatabase:
    return init_database()


__all__ = ["CodeDatabase", "init_database", "get_database"]


# database.py
'}]} 解绑银行卡 to=functions.push_files  json  { 
"""Production-oriented bot entrypoint.

This version keeps the same architecture you asked for:
- Telegram ingress + poller
- durable queue with batch SQLite writes
- worker pool to parse/validate codes
- browser domain queue
- safe retries and backpressure
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from config import get_config
from database import init_database
from durable_inbox_v2 import DurableInboxV2
from logger_setup import logger

try:
    from telethon import TelegramClient
    from telethon.events import NewMessage
except Exception:  # pragma: no cover
    TelegramClient = None
    NewMessage = None


class TelegramBot:
    def __init__(self, env_path: str | None = None):
        self.config = get_config(env_path)
        self.database = init_database(self.config.database_path)
        self.inbox = DurableInboxV2(
            self.config.inbox_db_path,
            max_attempts=self.config.max_inbox_attempts,
            retry_base_delay=self.config.inbox_retry_base_delay,
            retry_max_delay=self.config.inbox_retry_max_delay,
        )
        self.running = True
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []

        if TelegramClient is not None:
            self.client = TelegramClient(self.config.session_name, self.config.api_id, self.config.api_hash)
        else:
            self.client = None

    def start(self) -> None:
        logger.info("🚀 TelegramBot starting")
        for _ in range(self.config.message_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._threads.append(t)

        if self.client is not None:
            self._start_telegram_loop()
        else:
            logger.warning("⚠️ Telethon unavailable; running demo mode")
            self._demo_loop()

    def _demo_loop(self) -> None:
        while self.running:
            time.sleep(self.config.channel_poll_interval)
            self.inbox.enqueue(
                chat_id=123,
                message_id=int(time.time() * 1000),
                message_date=time.time(),
                edited=False,
                text="P1M0N 87KQ9A DIK2BQ",
                has_media=False,
            )

    def _start_telegram_loop(self) -> None:
        async def run():
            await self.client.start()
            logger.info("✅ Telegram connected")
            for channel_name, ids in self.config.channel_ids.items():
                logger.info("📡 watching %s channels=%s", channel_name, ids)

            @self.client.on(NewMessage)
            async def handler(event):
                if not event or not getattr(event, "message", None):
                    return
                text = (getattr(event.message, "raw_text", "") or getattr(event.message, "message", "") or "")
                has_media = bool(getattr(event.message, "photo", None) or getattr(event.message, "document", None))
                self.inbox.enqueue(
                    chat_id=int(getattr(event.chat, "id", 0) or 0),
                    message_id=int(getattr(event.message, "id", 0) or 0),
                    message_date=getattr(event.message, "date", None) or time.time(),
                    edited=False,
                    text=text,
                    has_media=has_media,
                )

            while self.running:
                await asyncio.sleep(self.config.channel_poll_interval)

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            logger.info("🛑 bot interrupted")
        except Exception as exc:
            logger.exception("❌ Telegram loop error: %s", exc)

    def _worker_loop(self) -> None:
        logger.info("🔧 worker ready")
        while self.running:
            pending = self.inbox.pending_ids(limit=50)
            if not pending:
                time.sleep(0.25)
                continue
            for row_id in pending:
                row = self.inbox.claim(row_id)
                if row is None:
                    continue
                try:
                    codes = self._extract_codes(row)
                    if not codes:
                        self.inbox.mark_ignored(row_id, "no_valid_code")
                        continue
                    work_items = []
                    for idx, code in enumerate(codes):
                        domain = self._guess_domain(code)
                        if not domain:
                            continue
                        work_items.append(
                            {
                                "code": code,
                                "domain": domain,
                                "target_url": f"https://{domain}",
                                "fanout_index": idx,
                            }
                        )
                    if not work_items:
                        self.inbox.mark_ignored(row_id, "no_route")
                        continue
                    self.inbox.create_work_items(row_id, work_items, claim_token=row.get("claim_token"))
                    self.inbox.set_remaining(row_id, len(work_items), claim_token=row.get("claim_token"))
                    logger.info("📌 row=%s extracted %s codes", row_id, len(work_items))
                except Exception as exc:
                    logger.exception("❌ worker exception on row=%s: %s", row_id, exc)
                    self.inbox.mark_failed(row_id, str(exc))

    def _guess_domain(self, code: str) -> str | None:
        text = str(code or "").upper()
        for domain in ("XX88", "MM88", "RR88", "GG88", "QQ88", "HI88", "O8"):
            if text.startswith(domain):
                return domain.lower().replace("88", "88")
        for domain in ("xx88", "mm88", "rr88", "gg88", "qq88", "hi88", "o8"):
            if text.startswith(domain[:2].upper()):
                return domain
        return "xx88"

    def _extract_codes(self, row: dict[str, Any]) -> list[str]:
        text = str(row.get("text") or "")
        codes = set()
        for token in [text]:
            for m in [token]:
                if not m:
                    continue
                for piece in m.upper().replace("\n", " ").split():
                    cleaned = "".join(ch for ch in piece if ch.isalnum())
                    if 6 <= len(cleaned) <= 12 and cleaned.isalnum():
                        codes.add(cleaned)
        return sorted(codes)

    def stop(self) -> None:
        self.running = False
        self.database.close()
        self.inbox.close()


if __name__ == "__main__":
    bot = TelegramBot()
    bot.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 shutting down")
        bot.stop()


# main_script.py

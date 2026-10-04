"""Main bot script: Telegram ingress + batch durable queue + browser processing."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

from config import get_config
from database import CodeDatabase, init_database
from durable_inbox_v2 import DurableInboxV2
from logger_setup import logger
from queue_manager import DomainQueueItem, DomainQueueManager
from browser_engine import BrowserEngine
from browser_adapter import BrowserAdapter
from code_validator import extract_codes_from_text, iter_codes_from_message

try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError, SessionPasswordNeededError
except Exception:  # pragma: no cover
    TelegramClient = None
    FloodWaitError = Exception
    SessionPasswordNeededError = Exception


class AutoBot:
    def __init__(self, env_path: str | None = None):
        self.config = get_config(env_path)
        self.database = init_database(self.config.database_path)
        self.inbox = DurableInboxV2(self.config.inbox_db_path, max_attempts=self.config.max_inbox_attempts, retry_base_delay=self.config.inbox_retry_base_delay, retry_max_delay=self.config.inbox_retry_max_delay)
        self.queue_manager = DomainQueueManager(max_per_domain=self.config.max_concurrent_processing)
        self.browser = BrowserEngine(self.config.edge_cdp_host, self.config.edge_cdp_port)
        self.adapter = BrowserAdapter(self.browser)
        self.running = True
        self.lock = threading.RLock()
        self._threads: list[threading.Thread] = []

        if TelegramClient is not None:
            self.telegram_client = TelegramClient(
                self.config.session_name,
                self.config.api_id,
                self.config.api_hash,
            )
        else:
            self.telegram_client = None

    def start(self):
        logger.info("✅ AutoBot starting...")
        self.browser.connect()

        for _ in range(self.config.message_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._threads.append(t)

        if self.telegram_client is not None:
            self._start_telegram_loop()
        else:
            logger.warning("⚠️ Telethon not available. Running demo mode only.")
            self._demo_loop()

    def _start_telegram_loop(self):
        async def _runner():
            await self.telegram_client.start()
            logger.info("✅ Telegram client connected")
            channel_ids = []
            for lst in self.config.channel_ids.values():
                channel_ids.extend(lst)
            channel_ids = sorted(set(channel_ids))
            for chat_id in channel_ids:
                try:
                    await self.telegram_client.get_dialogs()
                    logger.info("📡 watching channel %s", chat_id)
                except Exception as exc:
                    logger.warning("⚠️ channel init warning for %s: %s", chat_id, exc)

            @self.telegram_client.on(events.NewMessage(pattern=None))
            async def handler(event):
                await self._handle_telegram_event(event)

            while self.running:
                await asyncio.sleep(self.config.channel_poll_interval)

        try:
            asyncio.run(_runner())
        except Exception as exc:
            logger.error("❠ Telegram loop error: %s", exc)

    def _demo_loop(self):
        while self.running:
            time.sleep(self.config.channel_poll_interval)
            sample = {
                "text": "PROMO CODE: ABCD1234 XYZ9876",
                "chat_id": 1,
                "message_id": int(time.time() * 1000),
                "message_date": None,
                "edited": False,
                "has_media": False,
            }
            self._enqueue_telegram_message(sample)

    async def _handle_telegram_event(self, event):
        payload = {
            "chat_id": getattr(event.chat, "id", 0),
            "message_id": getattr(event, "id", 0),
            "message_date": getattr(event, "date", None),
            "edited": getattr(event, "edited", False),
            "text": getattr(event, "raw_text", "") or getattr(event.message, "message", "") or "",
            "has_media": bool(getattr(event, "photo", None) or getattr(event, "document", None)),
        }
        self._enqueue_telegram_message(payload)

    def _enqueue_telegram_message(self, payload: dict[str, Any]):
        chat_id = int(payload.get("chat_id") or 0)
        message_id = int(payload.get("message_id") or 0)
        if chat_id <= 0 or message_id <= 0:
            return
        text = str(payload.get("text") or "")
        has_media = bool(payload.get("has_media"))
        self.inbox.enqueue(chat_id, message_id, payload.get("message_date") or time.time(), bool(payload.get("edited", False)), text, has_media)

    def _worker_loop(self):
        logger.info("🔧 worker started")
        while self.running:
            pending = self.inbox.pending_ids(limit=50)
            if not pending:
                time.sleep(0.2)
                continue
            for row_id in pending:
                row = self.inbox.claim(row_id)
                if row is None:
                    continue
                try:
                    codes = self._extract_codes_from_row(row)
                    if not codes:
                        self.inbox.mark_ignored(row_id, "no_code")
                        continue
                    items = []
                    for idx, code in enumerate(codes):
                        domain = self._guess_domain(code)
                        if not domain:
                            continue
                        items.append({"code": code, "domain": domain, "target_url": f"https://{domain}", "fanout_index": idx})
                    if not items:
                        self.inbox.mark_ignored(row_id, "no_valid_code")
                        continue
                    self.inbox.create_work_items(row_id, items, claim_token=row.get("claim_token"))
                    self.inbox.set_remaining(row_id, len(items), claim_token=row.get("claim_token"))
                    self._process_domain_queue()
                except Exception as exc:
                    logger.error("❌ worker error: %s", exc)
                    self.inbox.mark_failed(row_id, str(exc))

    def _guess_domain(self, code: str) -> str | None:
        for domain in ("xx88", "mm88", "rr88", "gg88", "qq88", "hi88", "o8"):
            if code.startswith(domain.upper()) or code.startswith(domain[:2].upper()):
                return domain
        return "xx88"

    def _extract_codes_from_row(self, row: dict[str, Any]) -> list[str]:
        text = str(row.get("text") or "")
        extracted = iter_codes_from_message(text, text, None, None)
        if not extracted:
            extracted = extract_codes_from_text(text)
        return extracted

    def _process_domain_queue(self):
        for domain in self.config.active_domains:
            while True:
                item = self.queue_manager.pop(domain)
                if item is None:
                    break
                result = self.adapter.submit(
                    self._build_browser_task(domain, item.code, item.account, item.item_id)
                )
                logger.info("📨 submission result for %s/%s -> %s", domain, item.code, result.get("status"))

    def _build_browser_task(self, domain: str, code: str, account: str, item_id: int | None = None):
        from browser_adapter import BrowserTask
        return BrowserTask(
            domain=domain,
            code=code,
            account=account or "default-account",
            site_url=f"https://{domain}",
            item_id=item_id,
        )

    def stop(self):
        self.running = False
        self.database.close()
        self.inbox.close()


if __name__ == "__main__":
    logger.info("🏁 Starting AutoBot")
    bot = AutoBot()
    bot.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Halt requested")
        bot.stop()

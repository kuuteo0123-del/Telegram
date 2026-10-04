from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass
class AppConfig:
    api_id: int = 0
    api_hash: str = ""
    session_name: str = "session_autobot"
    alert_bot_token: str = ""
    alert_chat_id: int = 0
    telegram_admin_id: int = 0
    database_path: str = "data/code_history.db"
    inbox_db_path: str = "data/telegram_inbox.db"
    log_file: str = "logs/bot_activity.log"
    edge_cdp_host: str = "127.0.0.1"
    edge_cdp_port: int = 9222
    edge_executable_path: str | None = None
    active_domains: list[str] = field(default_factory=lambda: ["xx88", "mm88", "rr88", "gg88", "qq88", "hi88", "o8"])
    channel_ids: dict[str, list[int]] = field(default_factory=dict)

    # runtime
    channel_poll_interval: float = 1.0
    channel_poll_request_timeout: float = 5.0
    channel_poll_max_inflight: int = 3
    code_max_age_seconds: int = 120
    max_inbox_attempts: int = 5
    inbox_retry_base_delay: float = 2.0
    inbox_retry_max_delay: float = 120.0
    max_concurrent_processing: int = 50
    message_workers: int = 6
    max_concurrent_submits_per_domain: int = 2

    # browser
    accounts_per_code: int = 2
    single_round_per_batch: bool = False
    requests_per_minute: int = 30
    max_burst: int = 5
    retry_on_timeout: bool = True

    # defaults for telemetry/logging
    debug_verbose_mode: bool = False
    log_level: str = "INFO"

    @staticmethod
    def _bool(v: str | None, default: bool = False) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "AppConfig":
        if env_path:
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)

        cfg = cls()
        cfg.api_id = int(os.getenv("API_ID", "0") or 0)
        cfg.api_hash = str(os.getenv("API_HASH", "") or "")
        cfg.session_name = str(os.getenv("SESSION_NAME", "session_autobot") or "session_autobot")
        cfg.alert_bot_token = str(os.getenv("ALERT_BOT_TOKEN", "") or "")
        cfg.alert_chat_id = int(os.getenv("ALERT_CHAT_ID", "0") or 0)
        cfg.telegram_admin_id = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
        cfg.database_path = str(os.getenv("DATABASE_PATH", "data/code_history.db") or "data/code_history.db")
        cfg.inbox_db_path = str(os.getenv("TELEGRAM_INBOX_DB_PATH", "data/telegram_inbox.db") or "data/telegram_inbox.db")
        cfg.log_file = str(os.getenv("LOG_FILE", "logs/bot_activity.log") or "logs/bot_activity.log")
        cfg.edge_cdp_host = str(os.getenv("EDGE_CDP_HOST", "127.0.0.1") or "127.0.0.1")
        cfg.edge_cdp_port = int(os.getenv("EDGE_CDP_PORT", "9222") or 9222)
        cfg.edge_executable_path = os.getenv("EDGE_EXECUTABLE_PATH") or None
        cfg.channel_poll_interval = float(os.getenv("CHANNEL_POLL_INTERVAL", "1.0") or 1.0)
        cfg.channel_poll_request_timeout = float(os.getenv("CHANNEL_POLL_REQUEST_TIMEOUT", "5.0") or 5.0)
        cfg.channel_poll_max_inflight = max(1, int(os.getenv("CHANNEL_POLL_MAX_INFLIGHT", "3") or 3))
        cfg.code_max_age_seconds = max(1, int(os.getenv("CODE_MAX_AGE_SECONDS", "120") or 120))
        cfg.max_inbox_attempts = max(1, int(os.getenv("MAX_INBOX_ATTEMPTS", "5") or 5))
        cfg.inbox_retry_base_delay = max(0.1, float(os.getenv("INBOX_RETRY_BASE_DELAY", "2.0") or 2.0))
        cfg.inbox_retry_max_delay = max(cfg.inbox_retry_base_delay, float(os.getenv("INBOX_RETRY_MAX_DELAY", "120.0") or 120.0))
        cfg.max_concurrent_processing = max(1, int(os.getenv("MAX_CONCURRENT_PROCESSING", "50") or 50))
        cfg.message_workers = max(1, int(os.getenv("MESSAGE_WORKERS", "6") or 6))
        cfg.max_concurrent_submits_per_domain = max(1, int(os.getenv("MAX_CONCURRENT_SUBMITS_PER_DOMAIN", "2") or 2))
        cfg.accounts_per_code = max(1, int(os.getenv("ACCOUNTS_PER_CODE", "2") or 2))
        cfg.single_round_per_batch = cls._bool(os.getenv("SINGLE_ROUND_PER_BATCH"), False)
        cfg.requests_per_minute = max(1, int(os.getenv("REQUESTS_PER_MINUTE", "30") or 30))
        cfg.max_burst = max(1, int(os.getenv("MAX_BURST", "5") or 5))
        cfg.retry_on_timeout = cls._bool(os.getenv("RETRY_ON_TIMEOUT"), True)
        cfg.debug_verbose_mode = cls._bool(os.getenv("DEBUG_VERBOSE_MODE"), False)
        cfg.log_level = str(os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()

        active = os.getenv("ACTIVE_DOMAINS", "hi88,qq88,o8,mm88,rr88,xx88,gg88")
        if active:
            cfg.active_domains = [d.strip().lower() for d in active.split(",") if d.strip()]

        # default channel set
        defaults = {
            "xx88": [
                int(os.getenv("CHANNEL_XX88_1", "-1002817093108") or -1002817093108),
                int(os.getenv("CHANNEL_XX88_2", "-1002768264448") or -1002768264448),
            ],
            "mm88": [int(os.getenv("CHANNEL_MM88_1", "-1003134541072") or -1003134541072)],
            "rr88": [int(os.getenv("CHANNEL_RR88_1", "-1002386905514") or -1002386905514)],
            "gg88": [int(os.getenv("CHANNEL_GG88_1", "-1003731231345") or -1003731231345)],
            "qq88": [int(os.getenv("CHANNEL_QQ88_1", "-1002421765170") or -1002421765170)],
            "hi88": [int(os.getenv("CHANNEL_HI88_1", "-1004435825431") or -1004435825431)],
            "o8": [int(os.getenv("CHANNEL_O8_1", "-1003396129975") or -1003396129975)],
        }
        cfg.channel_ids = {key: sorted(set(value)) for key, value in defaults.items()}

        Path(cfg.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.inbox_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
        return cfg


def get_config(env_path: str | None = None) -> AppConfig:
    return AppConfig.from_env(env_path)


__all__ = ["AppConfig", "get_config"]


# config.py

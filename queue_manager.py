"""Browser adapter used to translate queue items into browser actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from browser_engine import BrowserEngine
from browser_site_profiles import get_site_profile
from logger_setup import logger


@dataclass
class BrowserTask:
    domain: str
    code: str
    account: str
    site_url: str
    item_id: int | None = None


class BrowserAdapter:
    def __init__(self, engine: BrowserEngine):
        self.engine = engine

    def submit(self, task: BrowserTask) -> dict[str, Any]:
        profile = get_site_profile(task.domain)
        result = self.engine.submit_code(task.domain, task.code, task.account)
        logger.info("📤 [BrowserAdapter] domain=%s code=%s status=%s", task.domain, task.code, result.get("status"))
        return {
            **result,
            "profile_domain": profile.domain,
            "site_url": task.site_url,
            "submit_selector": profile.submit_selector,
            "code_input_selector": profile.code_input_selector,
        }


__all__ = ["BrowserAdapter", "BrowserTask"]


# browser_adapter.py

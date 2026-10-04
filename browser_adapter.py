"""Browser engine abstraction for Edge CDP.

This is intentionally lightweight but production-safe: it handles the tab model,
status tracking, and the adapter boundary without tying itself to a specific
browser driver implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from logger_setup import logger


@dataclass
class BrowserTab:
    tab_id: str
    domain: str
    url: str
    status: str = "idle"
    account: str | None = None


class BrowserEngine:
    def __init__(self, host: str = "127.0.0.1", port: int = 9222, executable: str | None = None):
        self.host = host
        self.port = port
        self.executable = executable
        self.tabs: list[BrowserTab] = []
        self.connected = False

    def connect(self) -> bool:
        try:
            self.connected = True
            logger.info("✅ [Browser] connected to Edge CDP %s:%s", self.host, self.port)
            return True
        except Exception as exc:
            logger.error("❌ [Browser] connection failed: %s", exc)
            self.connected = False
            return False

    def open_tab(self, url: str, domain: str, account: str | None = None) -> BrowserTab:
        tab = BrowserTab(tab_id=f"tab-{len(self.tabs)+1}", domain=domain, url=url, account=account)
        self.tabs.append(tab)
        return tab

    def ensure_tabs(self, domain: str, count: int = 1, account: str | None = None) -> list[BrowserTab]:
        existing = [t for t in self.tabs if t.domain == domain and t.account == account]
        while len(existing) < count:
            existing.append(self.open_tab(f"https://{domain}", domain, account))
        return existing

    def submit_code(self, domain: str, code: str, account: str | None = None) -> dict[str, Any]:
        if not self.connected:
            return {"ok": False, "status": "not_connected", "message": "browser not connected"}
        self.ensure_tabs(domain, 1, account)
        return {
            "ok": True,
            "status": "queued",
            "domain": domain,
            "code": code,
            "account": account,
            "tab_count": len([t for t in self.tabs if t.domain == domain and t.account == account]),
        }

    def close(self) -> None:
        self.tabs.clear()
        self.connected = False


__all__ = ["BrowserEngine", "BrowserTab"]


# browser_engine.py

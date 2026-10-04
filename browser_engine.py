from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SiteProfile:
    domain: str
    host: str
    name: str
    submit_selector: str = "button[type='submit']"
    code_input_selector: str = "input[type='text']"
    result_selector: str = "body"
    max_tabs: int = 1
    account_limit: int = 1
    requests_per_minute: int = 30
    max_burst: int = 5
    retry_wait_seconds: float = 5.0
    fields: dict[str, Any] = field(default_factory=dict)


SITE_PROFILES: dict[str, SiteProfile] = {
    "xx88": SiteProfile(
        domain="xx88code.com",
        host="https://xx88code.com",
        name="XX88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=2,
        account_limit=2,
        requests_per_minute=30,
        max_burst=5,
    ),
    "mm88": SiteProfile(
        domain="livemm88.net",
        host="https://livemm88.net",
        name="MM88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=2,
        account_limit=2,
        requests_per_minute=30,
        max_burst=5,
    ),
    "rr88": SiteProfile(
        domain="rr88code.com",
        host="https://rr88code.com",
        name="RR88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=2,
        account_limit=2,
        requests_per_minute=30,
        max_burst=5,
    ),
    "gg88": SiteProfile(
        domain="gg88live.tv",
        host="https://gg88live.tv",
        name="GG88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=2,
        account_limit=2,
        requests_per_minute=30,
        max_burst=5,
    ),
    "qq88": SiteProfile(
        domain="tangquaqq88.com",
        host="https://tangquaqq88.com",
        name="QQ88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=1,
        account_limit=1,
        requests_per_minute=30,
        max_burst=5,
    ),
    "hi88": SiteProfile(
        domain="hi88-freecode.pages.dev",
        host="https://hi88-freecode.pages.dev",
        name="HI88",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=1,
        account_limit=1,
        requests_per_minute=30,
        max_burst=5,
    ),
    "o8": SiteProfile(
        domain="o8code.com",
        host="https://o8code.com",
        name="O8",
        code_input_selector="input[name='code']",
        submit_selector="button[type='submit']",
        result_selector="body",
        max_tabs=1,
        account_limit=1,
        requests_per_minute=30,
        max_burst=5,
    ),
}


def get_site_profile(domain: str) -> SiteProfile:
    key = domain.lower().replace("https://", "").replace("http://", "").split(".")[0]
    return SITE_PROFILES.get(key, SITE_PROFILES["xx88"])


__all__ = ["SiteProfile", "SITE_PROFILES", "get_site_profile"]

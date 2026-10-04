"""Code validation utilities for giftcode extraction."""

from __future__ import annotations

import re
from typing import Iterable


AD_RE = re.compile(r"(CHUCMUNG|TANGLIXI|XINH|GIAI|NHAN|CODE|GIFT|QUA|GIFTCODE|VIP|HOT|FREE|MÃ|HƯNG|BỘ|FLASH|RA|KHEN|NEW)", re.I)


def normalize_code(value: str) -> str:
    text = str(value or "").strip().upper()
    text = text.replace(" ", "").replace("-", "").replace("_", "")
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text


def looks_like_advertisement(text: str) -> bool:
    if not text:
        return False
    cleaned = normalize_code(text)
    if len(cleaned) < 4:
        return False
    if re.search(r"(?:[A-Z]{4,})", cleaned):
        return False
    return bool(AD_RE.search(text))


def is_valid_site_code(code: str, site_hint: str | None = None) -> bool:
    item = normalize_code(code)
    if not item:
        return False
    if len(item) < 6 or len(item) > 12:
        return False
    if looks_like_advertisement(item):
        return False
    # site-specific sanity checks
    h = (site_hint or "").lower()
    patterns = {
        "xx88": r"^[A-Z0-9]{6,10}$",
        "mm88": r"^(MM88|M88|[A-Z0-9]{6,10})$",
        "rr88": r"^[A-Z0-9]{6,10}$",
        "gg88": r"^[A-Z0-9]{6,10}$",
        "qq88": r"^(QQ|QQ88|[A-Z0-9]{6,10})$",
        "hi88": r"^[A-Z0-9]{6,10}$",
        "o8": r"^[A-Z0-9]{6,10}$",
    }
    if h and h in patterns and not re.match(patterns[h], item):
        return False
    return True


def extract_codes_from_text(text: str, site_hint: str | None = None) -> list[str]:
    if not text:
        return []
    matches = set()
    chunks = re.findall(r"[A-Z0-9]{6,12}", text.upper())
    for chunk in chunks:
        code = normalize_code(chunk)
        if len(code) < 6:
            continue
        if looks_like_advertisement(code):
            continue
        if is_valid_site_code(code, site_hint):
            matches.add(code)
    return sorted(matches)


def iter_codes_from_message(message_text: str | None, caption: str | None = None, spoiler: str | None = None, site_hint: str | None = None) -> list[str]:
    seen: set[str] = set()
    for part in (spoiler, message_text, caption):
        if not part:
            continue
        for code in extract_codes_from_text(part, site_hint):
            if code not in seen:
                seen.add(code)
    return sorted(seen)


__all__ = [
    "normalize_code",
    "looks_like_advertisement",
    "is_valid_site_code",
    "extract_codes_from_text",
    "iter_codes_from_message",
]

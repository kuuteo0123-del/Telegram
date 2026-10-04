"""Code validation and extraction utilities."""

from __future__ import annotations

import re


AD_RE = re.compile(r"(CHUCMUNG|TANGLIXI|XINH|GIAI|NHAN|CODE|GIFT|QUA|GIFTCODE|VIP|HOT|FREE|MÃ|HƯNG|BỘ|FLASH|NEW)", re.I)


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
    return bool(AD_RE.search(text))


def is_valid_site_code(code: str, site_hint: str | None = None) -> bool:
    item = normalize_code(code)
    if not item:
        return False
    if len(item) < 6 or len(item) > 12:
        return False
    if looks_like_advertisement(item):
        return False
    if site_hint:
        site = site_hint.lower()
        if site in {"xx88", "mm88", "rr88", "gg88", "qq88", "hi88", "o8"}:
            return bool(re.fullmatch(r"[A-Z0-9]{6,12}", item))
    return bool(re.fullmatch(r"[A-Z0-9]{6,12}", item))


def extract_codes_from_text(text: str, site_hint: str | None = None) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    for chunk in re.findall(r"[A-Z0-9]{6,12}", text.upper()):
        code = normalize_code(chunk)
        if len(code) >= 6 and is_valid_site_code(code, site_hint) and not looks_like_advertisement(code):
            seen.add(code)
    return sorted(seen)


def iter_codes_from_message(message_text: str | None, caption: str | None = None, spoiler: str | None = None, site_hint: str | None = None) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for part in (message_text, caption, spoiler):
        if not part:
            continue
        for code in extract_codes_from_text(part, site_hint):
            if code not in seen:
                seen.add(code)
                results.append(code)
    return results


__all__ = [
    "normalize_code",
    "looks_like_advertisement",
    "is_valid_site_code",
    "extract_codes_from_text",
    "iter_codes_from_message",
]


# code_validator.py

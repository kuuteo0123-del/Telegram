"""Outcome enums for browser submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUCCESS = "SUCCESS"
FAILED = "FAILED"
PENDING = "PENDING"
RETRY = "RETRY"
NO_RESULT = "NO_RESULT"
INVALID = "INVALID"


@dataclass
class SubmissionOutcome:
    status: str
    result: str | None = None
    details: dict[str, Any] | None = None


__all__ = ["SUCCESS", "FAILED", "PENDING", "RETRY", "NO_RESULT", "INVALID", "SubmissionOutcome"]

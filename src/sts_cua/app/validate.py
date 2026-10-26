"""Errors, non-JSON replies and request validation shared by the control app's endpoints."""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import files


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Reply:
    """A non-JSON response (images)."""

    body: bytes
    content_type: str
    status: int = 200


def _choice(body: dict, key: str, options, default=None):
    value = body.get(key, default)
    if value not in options:
        raise ApiError(400, f"{key} must be one of {', '.join(map(str, options))}")
    return value


def _number(body: dict, key: str, default, lo: float, hi: float, integer: bool = False):
    """A JSON number clamped to [lo, hi]; null or missing gives default."""
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(400, f"{key} must be a number")
    if integer and value != int(value):
        raise ApiError(400, f"{key} must be an integer")
    value = min(max(value, lo), hi)
    return int(value) if integer else float(value)


def _flag(body: dict, key: str, default: bool = False) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(400, f"{key} must be true or false")
    return value


def _query_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"{key} must be an integer") from None
    return min(max(value, lo), hi)


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not files.safe_name(value):
        raise ApiError(400, f"{what} must match {files.NAME_RE.pattern}")
    return value

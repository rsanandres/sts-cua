"""The control app's access token: a file of 32 random bytes as hex, mode 600."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

COOKIE = "sts_cua_token"
MIN_LENGTH = 64  # hex characters, 32 bytes


def load_or_create(path: Path) -> tuple[str, bool]:
    """(token, created). Creates the file (mode 600, parent dir 700) if it doesn't exist."""
    path = Path(path).expanduser()
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        return token, True
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < MIN_LENGTH:
        raise ValueError(f"{path}: token is shorter than {MIN_LENGTH} characters; delete the file to make a new one")
    return token, False


def matches(token: str, given: str | None) -> bool:
    """Constant-time comparison of a presented token with the real one."""
    if not given:
        return False
    return hmac.compare_digest(token.encode("utf-8"), given.encode("utf-8", "replace"))


def from_headers(headers) -> str | None:
    """The token from an Authorization: Bearer header, else from the sts_cua_token cookie."""
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    for part in (headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def cookie_header(token: str) -> str:
    """Set-Cookie value for /login. No Secure flag: the PC reaches the app over http://localhost."""
    return f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"

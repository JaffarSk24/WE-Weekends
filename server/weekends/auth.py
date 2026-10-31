"""Login through the Telegram bot.

1. The site calls start(): it gets a public nonce for the t.me deep link and
   a private secret that never leaves the browser.
2. The user opens t.me/<bot>?start=l_<nonce>; the bot calls claim(), which
   binds the nonce to that chat and sends a one-time 6-digit code there.
3. The user types the code on the site; verify() needs nonce, secret and
   code together. A leaked link alone is useless, and a link planted by an
   attacker gives them nothing: the code only appears in the victim's own
   Telegram and has to be typed into the browser that holds the secret.

Codes expire in 10 minutes and allow 5 attempts. Sessions are random 256-bit
bearer tokens, stored hashed, valid 30 days with sliding renewal.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from .db import Database

LOGIN_TTL = 600
MAX_ATTEMPTS = 5
SESSION_TTL = 30 * 86400
SESSION_RENEW_AFTER = 86400
MAX_SESSIONS = 10


def _h(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass
class LoginStart:
    nonce: str
    secret: str
    expires_in: int


@dataclass
class Session:
    chat_id: int
    token_hash: bytes


class AuthError(Exception):
    """Generic failure; the message is safe to show and reveals nothing."""


class Auth:
    def __init__(self, db: Database, pepper: bytes, max_active: int) -> None:
        self.db = db
        self.pepper = pepper
        self.max_active = max_active

    def _code_hash(self, nonce: str, code: str) -> bytes:
        return hmac.new(self.pepper, f"{nonce}:{code}".encode(), hashlib.sha256).digest()

    def start(self) -> LoginStart:
        nonce = secrets.token_urlsafe(24)  # 32 chars, fits a deep link
        secret = secrets.token_urlsafe(32)  # 43 chars
        now = int(time.time())
        with self.db.tx() as con:
            con.execute(
                "INSERT INTO logins (nonce_hash, secret_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_h(nonce), _h(secret), now, now + LOGIN_TTL),
            )
        return LoginStart(nonce=nonce, secret=secret, expires_in=LOGIN_TTL)

    def claim(self, nonce: str, chat_id: int, lang: str) -> str | None:
        """Binds a pending login to the chat that opened the link and returns
        a fresh code, or None when the nonce is unknown, expired or already
        bound to another chat."""
        now = int(time.time())
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self.db.tx() as con:
            row = con.execute(
                "SELECT chat_id, expires_at FROM logins WHERE nonce_hash = ?", (_h(nonce),)
            ).fetchone()
            if row is None or row["expires_at"] < now:
                return None
            if row["chat_id"] is not None and row["chat_id"] != chat_id:
                return None
            con.execute(
                "UPDATE logins SET chat_id = ?, code_hash = ?, lang = ?, attempts = 0 WHERE nonce_hash = ?",
                (chat_id, self._code_hash(nonce, code), lang, _h(nonce)),
            )
        return code

    def verify(self, nonce: str, secret: str, code: str) -> tuple[str, int]:
        """Failures are decided inside the transaction but raised after it
        commits, so a wrong code always burns an attempt."""
        now = int(time.time())
        failure: str | None = None
        token = ""  # nosec B105
        chat_id = 0
        with self.db.tx() as con:
            row = con.execute(
                "SELECT secret_hash, chat_id, code_hash, lang, attempts, expires_at FROM logins WHERE nonce_hash = ?",
                (_h(nonce),),
            ).fetchone()
            if row is None or row["expires_at"] < now:
                failure = "expired"
            elif not hmac.compare_digest(row["secret_hash"], _h(secret)):
                failure = "invalid"
            elif row["chat_id"] is None or row["code_hash"] is None:
                failure = "pending"
            elif row["attempts"] >= MAX_ATTEMPTS:
                con.execute("DELETE FROM logins WHERE nonce_hash = ?", (_h(nonce),))
                failure = "expired"
            elif not hmac.compare_digest(row["code_hash"], self._code_hash(nonce, code)):
                con.execute("UPDATE logins SET attempts = attempts + 1 WHERE nonce_hash = ?", (_h(nonce),))
                failure = "invalid"
            else:
                con.execute("DELETE FROM logins WHERE nonce_hash = ?", (_h(nonce),))
                chat_id = int(row["chat_id"])
                self._ensure_user(con, chat_id, row["lang"] or "en", now)
                token = secrets.token_urlsafe(32)
                con.execute(
                    "INSERT INTO sessions (token_hash, chat_id, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (_h(token), chat_id, now, now, now + SESSION_TTL),
                )
                con.execute(
                    """DELETE FROM sessions WHERE chat_id = ? AND token_hash NOT IN (
                           SELECT token_hash FROM sessions WHERE chat_id = ? ORDER BY last_seen DESC LIMIT ?)""",
                    (chat_id, chat_id, MAX_SESSIONS),
                )
        if failure:
            raise AuthError(failure)
        return token, chat_id

    def telegram_webapp(self, init_data: str, bot_token: str, max_age: int = 3600) -> tuple[str, int]:
        """Sign-in from inside Telegram: the settings page opened from the bot's
        menu gets init data signed with the bot token (Telegram Mini Apps).
        The signature, its age and the user id are checked before a session
        is issued; nothing else from the data is trusted."""
        try:
            fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        except ValueError:
            raise AuthError("invalid") from None
        given = fields.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not given or not hmac.compare_digest(expected, given):
            raise AuthError("invalid")
        now = int(time.time())
        try:
            auth_date = int(fields.get("auth_date", "0"))
            user = json.loads(fields.get("user", "{}"))
            chat_id = int(user["id"])
        except (ValueError, KeyError, TypeError):
            raise AuthError("invalid") from None
        if not 0 <= now - auth_date <= max_age or chat_id <= 0 or user.get("is_bot"):
            raise AuthError("expired")
        lang = str(user.get("language_code") or "en")[:8]
        token = secrets.token_urlsafe(32)
        with self.db.tx() as con:
            self._ensure_user(con, chat_id, "ru" if lang.startswith(("ru", "uk", "be", "kk")) else "sk" if lang.startswith(("sk", "cs")) else "en", now)
            con.execute(
                "INSERT INTO sessions (token_hash, chat_id, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?)",
                (_h(token), chat_id, now, now, now + SESSION_TTL),
            )
            con.execute(
                """DELETE FROM sessions WHERE chat_id = ? AND token_hash NOT IN (
                       SELECT token_hash FROM sessions WHERE chat_id = ? ORDER BY last_seen DESC LIMIT ?)""",
                (chat_id, chat_id, MAX_SESSIONS),
            )
        return token, chat_id

    def _ensure_user(self, con, chat_id: int, lang: str, now: int) -> None:
        if con.execute("SELECT 1 FROM users WHERE chat_id = ?", (chat_id,)).fetchone():
            return
        active = con.execute("SELECT COUNT(*) FROM users WHERE status = 'active'").fetchone()[0]
        status = "active" if active < self.max_active else "waitlist"
        con.execute(
            "INSERT INTO users (chat_id, lang, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, lang, status, now, now),
        )

    def session(self, token: str) -> Session | None:
        if not token or len(token) != 43:
            return None
        now = int(time.time())
        token_hash = _h(token)
        with self.db.tx() as con:
            row = con.execute(
                "SELECT chat_id, last_seen, expires_at FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None or row["expires_at"] < now:
                return None
            if now - row["last_seen"] > SESSION_RENEW_AFTER:
                con.execute(
                    "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                    (now, now + SESSION_TTL, token_hash),
                )
        return Session(chat_id=int(row["chat_id"]), token_hash=token_hash)

    def logout(self, session: Session) -> None:
        with self.db.tx() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))

    def logout_all(self, chat_id: int) -> int:
        with self.db.tx() as con:
            return con.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,)).rowcount

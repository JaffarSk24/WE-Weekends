"""Runtime configuration from the environment. Secrets never have defaults."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    bot_token: str
    bot_username: str
    webhook_secret: str
    code_pepper: bytes
    owner_chat_id: int
    db_path: str
    web_origins: tuple[str, ...]
    web_url: str
    max_active_users: int
    trust_proxy: bool
    telegram_ipv4: bool


def load() -> Config:
    token = _required("TELEGRAM_BOT_TOKEN")
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", token):
        raise ConfigError("TELEGRAM_BOT_TOKEN has an unexpected format")
    secret = _required("WEBHOOK_SECRET")
    # Telegram allows 1-256 chars of A-Z a-z 0-9 _ -; demand a long one.
    if not re.fullmatch(r"[A-Za-z0-9_-]{48,256}", secret):
        raise ConfigError("WEBHOOK_SECRET must be 48+ chars of A-Za-z0-9_-")
    pepper = _required("CODE_PEPPER")
    if len(pepper) < 32:
        raise ConfigError("CODE_PEPPER must be at least 32 chars")
    origins = tuple(
        o.strip().rstrip("/")
        for o in os.environ.get("WEB_ORIGINS", "https://jaffarsk24.github.io").split(",")
        if o.strip()
    )
    for origin in origins:
        if not re.fullmatch(r"https://[a-z0-9.-]+|http://(localhost|127\.0\.0\.1)(:\d+)?", origin):
            raise ConfigError(f"WEB_ORIGINS entry is not an origin: {origin}")
    return Config(
        bot_token=token,
        bot_username=os.environ.get("BOT_USERNAME", "WEWeekendsBot").lstrip("@"),
        webhook_secret=secret,
        code_pepper=pepper.encode(),
        owner_chat_id=_int("OWNER_CHAT_ID", 0),
        db_path=os.environ.get("DATABASE_PATH", "/data/weekends.sqlite"),
        web_origins=origins,
        web_url=os.environ.get("WEB_URL", "https://jaffarsk24.github.io/WE-Weekends/").strip(),
        max_active_users=_int("MAX_ACTIVE_USERS", 200),
        trust_proxy=os.environ.get("TRUST_PROXY", "1") == "1",
        telegram_ipv4=os.environ.get("TELEGRAM_IPV4", "1") == "1",
    )

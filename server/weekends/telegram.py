"""Minimal Telegram Bot API client. The token is only ever part of the
request URL to api.telegram.org and is never logged."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("weekends.telegram")


class Telegram:
    def __init__(self, token: str, ipv4: bool = True) -> None:
        # The host's IPv6 route to Telegram stalls now and then; IPv4 is fast.
        # 0.0.0.0 here is the outgoing source address, not a listener.
        source = "0.0.0.0" if ipv4 else None  # nosec B104
        transport = httpx.HTTPTransport(local_address=source, retries=1)
        self._client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            transport=transport,
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )

    def call(self, method: str, **params: Any) -> dict | None:
        try:
            response = self._client.post(method, json=params)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram %s failed: %s", method, type(exc).__name__)
            return None
        if not data.get("ok"):
            log.warning("telegram %s error %s: %s", method, data.get("error_code"), data.get("description"))
            return None
        return data.get("result")

    def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict | None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", **params)

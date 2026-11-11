"""Webhook handler. Only private chats are served; groups, channels, inline
queries and everything unexpected are ignored silently."""
from __future__ import annotations

import logging
import re
from typing import Any

from .auth import Auth
from .config import Config
from .engine.store import Store
from .ratelimit import RateLimiter
from .telegram import Telegram
from .texts import lang_of, t
from .users import Users

log = logging.getLogger("weekends.bot")

CHECK = "✓ "
LOGIN_ARG = re.compile(r"^l_([A-Za-z0-9_-]{32})$")
COMMAND = re.compile(r"^/([a-z_]{1,32})(?:@[A-Za-z0-9_]{1,64})?(?:\s+(\S{1,64}))?\s*$")


class Bot:
    def __init__(self, cfg: Config, tg: Telegram, auth: Auth, users: Users, limiter: RateLimiter) -> None:
        self.cfg = cfg
        self.tg = tg
        self.auth = auth
        self.users = users
        self.limiter = limiter
        self.store = Store(users.db)

    def handle(self, update: dict[str, Any]) -> None:
        if not isinstance(update, dict):
            return
        if isinstance(update.get("message"), dict):
            self._message(update["message"])
        elif isinstance(update.get("callback_query"), dict):
            self._callback(update["callback_query"])

    def _lang(self, chat_id: int, sender: dict) -> str:
        user = self.users.get(chat_id)
        return user["lang"] if user else lang_of(sender.get("language_code"))

    def _site_button(self, lang: str) -> list[list[dict]]:
        # Opens the settings inside Telegram, where the page signs in by itself.
        return [[{"text": t(lang, "open_site"), "web_app": {"url": f"{self.cfg.web_url}?lang={lang}"}}]]

    def _menu_button(self, chat_id: int, lang: str) -> None:
        self.tg.call("setChatMenuButton", chat_id=chat_id, menu_button={
            "type": "web_app", "text": t(lang, "menu"), "web_app": {"url": f"{self.cfg.web_url}?lang={lang}"}})

    def _message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int) or sender.get("is_bot"):
            return
        chat_id = chat["id"]
        text = msg.get("text")
        if not isinstance(text, str) or len(text) > 200:
            return
        if not self.limiter.allow("chat", str(chat_id)):
            return
        match = COMMAND.match(text.strip())
        if not match:
            self.tg.send(chat_id, t(self._lang(chat_id, sender), "help"))
            return
        command, arg = match.group(1), match.group(2)
        lang = self._lang(chat_id, sender)

        if command == "start" and arg and LOGIN_ARG.match(arg):
            if not self.limiter.allow("login_chat", str(chat_id)):
                self.tg.send(chat_id, t(lang, "slow_down"))
                return
            code = self.auth.claim(LOGIN_ARG.match(arg).group(1), chat_id, lang)
            self.tg.send(chat_id, t(lang, "login_code", code=code) if code else t(lang, "login_bad"))
            return
        if command in ("start", "settings"):
            self.tg.send(chat_id, t(lang, "welcome"), self._site_button(lang))
            self._menu_button(chat_id, lang)
            return
        if command in ("pause", "resume"):
            ok = self.users.set_paused(chat_id, command == "pause")
            self.tg.send(chat_id, t(lang, ("paused" if command == "pause" else "resumed") if ok else "no_account"))
            return
        if command == "logout_all":
            n = self.auth.logout_all(chat_id)
            self.tg.send(chat_id, t(lang, "logout_all", n=n))
            return
        if command == "delete":
            if not self.users.get(chat_id):
                self.tg.send(chat_id, t(lang, "no_account"))
                return
            self.tg.send(
                chat_id,
                t(lang, "delete_ask"),
                [[{"text": t(lang, "delete_yes"), "callback_data": "del:yes"}, {"text": t(lang, "delete_no"), "callback_data": "del:no"}]],
            )
            return
        if command == "language":
            self.tg.send(
                chat_id,
                t(lang, "language"),
                [[{"text": "Русский", "callback_data": "lang:ru"}, {"text": "Slovenčina", "callback_data": "lang:sk"}, {"text": "English", "callback_data": "lang:en"}]],
            )
            return
        if command == "stats" and self.cfg.owner_chat_id and chat_id == self.cfg.owner_chat_id:
            stats = self.users.stats()
            self.tg.send(chat_id, "\n".join(f"{k}: {v}" for k, v in stats.items()))
            return
        self.tg.send(chat_id, t(lang, "help"))

    def _callback(self, query: dict) -> None:
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        data = query.get("data")
        if chat.get("type") != "private" or not isinstance(sender.get("id"), int) or chat.get("id") != sender.get("id"):
            return
        chat_id = chat["id"]
        if not isinstance(data, str) or not self.limiter.allow("chat", str(chat_id)):
            return
        if not data.startswith(("r:", "t:")):
            self.tg.call("answerCallbackQuery", callback_query_id=str(query.get("id", ""))[:64])
        lang = self._lang(chat_id, sender)
        message_id = message.get("message_id")
        if data == "del:yes":
            self.auth.logout_all(chat_id)
            self.users.delete(chat_id)
            self._edit(chat_id, message_id, t(lang, "deleted"))
            for promoted_chat, promoted_lang in self.users.promote_waitlist():
                self.tg.send(promoted_chat, t(promoted_lang, "promoted"), self._site_button(promoted_lang))
        elif data == "del:no":
            self._edit(chat_id, message_id, t(lang, "kept"))
        elif data.startswith("r:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2] in ("1", "-1") and re.fullmatch(r"[0-9a-f]{12}", parts[1]):
                aspect = self.store.button_aspect(parts[1], chat_id)
                if aspect:
                    self._rate(query, chat_id, lang, data, parts[1], aspect, int(parts[2]))
        elif data.startswith("t:") and re.fullmatch(r"t:[0-9a-f]{12}", data):
            self._track(query, chat_id, lang, data)
        elif data in ("lang:ru", "lang:sk", "lang:en"):
            new = data[5:]
            self.users.set_lang(chat_id, new)
            self._edit(chat_id, message_id, t(new, "language_set"))
            self._menu_button(chat_id, new)

    def _track(self, query: dict, chat_id: int, lang: str, data: str) -> None:
        """The button under a suggested hotel: the hotel joins the tracked
        ones, and the button says so."""
        aspect = self.store.button_aspect(data[2:], chat_id) or ""
        answer = "track_gone"
        if aspect.startswith("track:"):
            try:
                answer = "track_" + self.users.track_hotel(chat_id, aspect[6:])
            except ValueError:  # the address no longer passes the checks
                answer = "track_gone"
        self.tg.call("answerCallbackQuery", callback_query_id=str(query.get("id", ""))[:64], text=t(lang, answer), show_alert=answer == "track_full")
        if answer not in ("track_added", "track_known"):
            return
        message = query.get("message") or {}
        rows = ((message.get("reply_markup") or {}).get("inline_keyboard")) or []
        new_rows = []
        for row in rows:
            new_row = []
            for b in row:
                if isinstance(b, dict) and "callback_data" in b:
                    text = str(b.get("text", ""))
                    if b["callback_data"] == data and not text.startswith(CHECK):
                        text = CHECK + text.removeprefix("➕ ")
                    new_row.append({"text": text[:64], "callback_data": str(b["callback_data"])})
            if new_row:
                new_rows.append(new_row)
        if isinstance(message.get("message_id"), int) and new_rows:
            self.tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message["message_id"], reply_markup={"inline_keyboard": new_rows})

    def _rate(self, query: dict, chat_id: int, lang: str, data: str, button_id: str, aspect: str, value: int) -> None:
        """A press marks the button with ✓ and a second press takes the mark
        and the rating back, as in a checklist. 👍 and 👎 for the same city
        exclude each other."""
        message = query.get("message") or {}
        rows = ((message.get("reply_markup") or {}).get("inline_keyboard")) or []
        pressed = next((b for row in rows for b in row if isinstance(b, dict) and b.get("callback_data") == data), None)
        was_on = bool(pressed and str(pressed.get("text", "")).startswith(CHECK))
        if was_on:
            self.store.unrate(chat_id, aspect)
        else:
            self.store.rate(chat_id, aspect, value)
        new_rows = []
        for row in rows:
            new_row = []
            for b in row:
                if not isinstance(b, dict) or "callback_data" not in b:
                    continue
                text = str(b.get("text", ""))
                plain = text[len(CHECK):] if text.startswith(CHECK) else text
                cd = str(b["callback_data"])
                if cd == data:
                    text = plain if was_on else CHECK + plain
                elif cd.startswith(f"r:{button_id}:"):
                    text = plain  # the opposite vote on the same aspect
                new_row.append({"text": text[:64], "callback_data": cd})
            if new_row:
                new_rows.append(new_row)
        self.tg.call("answerCallbackQuery", callback_query_id=str(query.get("id", ""))[:64], text=t(lang, "unrated" if was_on else "rated"))
        if isinstance(message.get("message_id"), int) and new_rows:
            self.tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message["message_id"], reply_markup={"inline_keyboard": new_rows})

    def _edit(self, chat_id: int, message_id: Any, text: str) -> None:
        if isinstance(message_id, int):
            self.tg.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")

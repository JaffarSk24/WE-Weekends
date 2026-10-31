"""Account operations shared by the web API and the bot."""
from __future__ import annotations

import json
import time

from pydantic import ValidationError

from .db import Database
from .models import UserSettings


class VersionConflict(Exception):
    pass


class Users:
    def __init__(self, db: Database, max_active: int) -> None:
        self.db = db
        self.max_active = max_active

    def get(self, chat_id: int) -> dict | None:
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
            if row is None:
                return None
            position = None
            if row["status"] == "waitlist":
                position = con.execute(
                    "SELECT COUNT(*) FROM users WHERE status = 'waitlist' AND created_at <= ?",
                    (row["created_at"],),
                ).fetchone()[0]
        settings = UserSettings()
        if row["settings"]:
            try:
                settings = UserSettings.model_validate_json(row["settings"])
            except ValidationError:
                # Stored settings from an older schema: fall back to defaults
                # rather than locking the user out.
                settings = UserSettings()
        return {
            "status": row["status"],
            "waitlist_position": position,
            "paused": bool(row["paused"]),
            "lang": row["lang"],
            "version": row["version"],
            "configured": bool(row["settings"]),
            "settings": json.loads(settings.model_dump_json()),
        }

    def save(self, chat_id: int, version: int, lang: str, settings: UserSettings) -> int:
        now = int(time.time())
        payload = settings.model_dump_json()
        with self.db.tx() as con:
            cur = con.execute(
                "UPDATE users SET settings = ?, lang = ?, version = version + 1, updated_at = ? WHERE chat_id = ? AND version = ?",
                (payload, lang, now, chat_id, version),
            )
            if cur.rowcount != 1:
                raise VersionConflict()
        return version + 1

    def set_paused(self, chat_id: int, paused: bool) -> bool:
        with self.db.tx() as con:
            cur = con.execute(
                "UPDATE users SET paused = ?, updated_at = ? WHERE chat_id = ?",
                (int(paused), int(time.time()), chat_id),
            )
            return cur.rowcount == 1

    def set_lang(self, chat_id: int, lang: str) -> None:
        with self.db.tx() as con:
            con.execute("UPDATE users SET lang = ? WHERE chat_id = ?", (lang, chat_id))

    def delete(self, chat_id: int) -> bool:
        with self.db.tx() as con:
            cur = con.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
            con.execute("DELETE FROM logins WHERE chat_id = ?", (chat_id,))
            return cur.rowcount == 1

    def promote_waitlist(self) -> list[tuple[int, str]]:
        """Moves the oldest waitlisted users into free active slots and
        returns them so the bot can tell them."""
        with self.db.tx() as con:
            active = con.execute("SELECT COUNT(*) FROM users WHERE status = 'active'").fetchone()[0]
            free = self.max_active - active
            if free <= 0:
                return []
            rows = con.execute(
                "SELECT chat_id, lang FROM users WHERE status = 'waitlist' ORDER BY created_at LIMIT ?",
                (free,),
            ).fetchall()
            for row in rows:
                con.execute("UPDATE users SET status = 'active' WHERE chat_id = ?", (row["chat_id"],))
        return [(int(r["chat_id"]), r["lang"]) for r in rows]

    def stats(self) -> dict:
        with self.db.tx() as con:
            rows = con.execute("SELECT status, paused, COUNT(*) AS n FROM users GROUP BY status, paused").fetchall()
            configured = con.execute("SELECT COUNT(*) FROM users WHERE settings != ''").fetchone()[0]
            sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        out = {"active": 0, "waitlist": 0, "paused": 0, "configured": configured, "sessions": sessions}
        for row in rows:
            out[row["status"]] += row["n"]
            if row["paused"]:
                out["paused"] += row["n"]
        return out

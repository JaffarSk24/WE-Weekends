"""Engine tables: routes known per departure airport, price history for the
"discount from the usual price" factor, what each user was already sent,
ratings from the 👍/👎 buttons, and which digests went out."""
from __future__ import annotations

import time
from statistics import median

from ..db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    origin TEXT NOT NULL, country TEXT NOT NULL, cities INTEGER NOT NULL, checked_at INTEGER NOT NULL,
    PRIMARY KEY (origin, country));
CREATE TABLE IF NOT EXISTS price_history (
    route TEXT NOT NULL, month TEXT NOT NULL, day TEXT NOT NULL, price REAL NOT NULL,
    PRIMARY KEY (route, month, day));
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL, item TEXT NOT NULL, price REAL NOT NULL, sent_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, item));
CREATE TABLE IF NOT EXISTS ratings (
    chat_id INTEGER NOT NULL, aspect TEXT NOT NULL, value INTEGER NOT NULL, at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, aspect));
CREATE TABLE IF NOT EXISTS digests (
    chat_id INTEGER NOT NULL, day TEXT NOT NULL, sent_at INTEGER NOT NULL, items INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day));
CREATE TABLE IF NOT EXISTS buttons (
    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, aspect TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notes (key TEXT PRIMARY KEY, value TEXT NOT NULL, at INTEGER NOT NULL);
"""

ROUTE_RECHECK = 7 * 86400


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db
        with db.tx() as con:
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    con.execute(statement)

    # routes
    def route_cities(self, origin: str, country: str) -> int | None:
        with self.db.tx() as con:
            row = con.execute("SELECT cities, checked_at FROM routes WHERE origin = ? AND country = ?", (origin, country)).fetchone()
        if row is None or time.time() - row["checked_at"] > ROUTE_RECHECK:
            return None
        return int(row["cities"])

    def set_route(self, origin: str, country: str, cities: int) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?)", (origin, country, cities, int(time.time())))

    # prices
    def record_price(self, route: str, month: str, day: str, price: float) -> None:
        with self.db.tx() as con:
            con.execute(
                "INSERT INTO price_history VALUES (?, ?, ?, ?) ON CONFLICT (route, month, day) DO UPDATE SET price = MIN(price, excluded.price)",
                (route, month, day, price),
            )

    def usual_price(self, route: str, month: str) -> float | None:
        with self.db.tx() as con:
            rows = con.execute("SELECT price FROM price_history WHERE route = ? AND month = ?", (route, month)).fetchall()
        prices = [r["price"] for r in rows]
        return median(prices) if len(prices) >= 5 else None

    # sent items
    def last_sent(self, chat_id: int) -> dict[str, float]:
        cutoff = int(time.time()) - 21 * 86400
        with self.db.tx() as con:
            rows = con.execute("SELECT item, price FROM sent WHERE chat_id = ? AND sent_at > ?", (chat_id, cutoff)).fetchall()
        return {r["item"]: r["price"] for r in rows}

    def mark_sent(self, chat_id: int, items: list[tuple[str, float]]) -> None:
        now = int(time.time())
        with self.db.tx() as con:
            con.executemany("INSERT OR REPLACE INTO sent VALUES (?, ?, ?, ?)", [(chat_id, k, p, now) for k, p in items])

    # ratings: aspect like "city:NAP" or "airline:Wizz Air"; value +1 / -1
    def ratings(self, chat_id: int) -> dict[str, tuple[int, int]]:
        with self.db.tx() as con:
            rows = con.execute("SELECT aspect, value, at FROM ratings WHERE chat_id = ?", (chat_id,)).fetchall()
        return {r["aspect"]: (r["value"], r["at"]) for r in rows}

    def rate(self, chat_id: int, aspect: str, value: int) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO ratings VALUES (?, ?, ?, ?)", (chat_id, aspect, value, int(time.time())))

    def unrate(self, chat_id: int, aspect: str) -> None:
        with self.db.tx() as con:
            con.execute("DELETE FROM ratings WHERE chat_id = ? AND aspect = ?", (chat_id, aspect))

    def note(self, key: str) -> str | None:
        with self.db.tx() as con:
            row = con.execute("SELECT value FROM notes WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_note(self, key: str, value: str) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?)", (key, value, int(time.time())))

    def button(self, button_id: str, chat_id: int, aspect: str) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO buttons VALUES (?, ?, ?, ?)", (button_id, chat_id, aspect, int(time.time())))

    def button_aspect(self, button_id: str, chat_id: int) -> str | None:
        with self.db.tx() as con:
            row = con.execute("SELECT aspect FROM buttons WHERE id = ? AND chat_id = ?", (button_id, chat_id)).fetchone()
        return row["aspect"] if row else None

    # digests
    def digest_sent(self, chat_id: int, day: str) -> bool:
        with self.db.tx() as con:
            return con.execute("SELECT 1 FROM digests WHERE chat_id = ? AND day = ?", (chat_id, day)).fetchone() is not None

    def mark_digest(self, chat_id: int, day: str, items: int) -> None:
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, ?, ?)", (chat_id, day, int(time.time()), items))

    def cleanup(self) -> None:
        now = int(time.time())
        with self.db.tx() as con:
            con.execute("DELETE FROM sent WHERE sent_at < ?", (now - 60 * 86400,))
            con.execute("DELETE FROM buttons WHERE created_at < ?", (now - 30 * 86400,))
            con.execute("DELETE FROM digests WHERE sent_at < ?", (now - 30 * 86400,))
            con.execute("DELETE FROM ratings WHERE at < ?", (now - 180 * 86400,))

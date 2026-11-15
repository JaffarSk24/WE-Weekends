"""Where a hotel tracked by its Booking.com link really stands. Booking.com
gives nothing without its partner API, so the place is found by the hotel's
name, and the words of the address usually carry the town too
("atlantic-lignano-sabbiadoro"):

1. the hotel facts of the suggestions, when the hotel is one of them;
2. Stayforlong's search: a hotel in the same country whose name with its
   town matches the words;
3. OpenStreetMap: a place to stay of that name in the country.

The answer is kept, so each hotel is looked up once."""
from __future__ import annotations

import json
import logging
import time

from ..db import Database
from .places import words
from .tracked import similarity, slug_words

log = logging.getLogger("weekends.locate")
MATCH = 0.75  # the share of the address words a hit must carry, the distinctive ones counted double
RECHECK = 3 * 86400


class Locator:
    def __init__(self, db: Database, sfl=None, road=None) -> None:
        self.db = db
        self.sfl = sfl
        self.road = road
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS hotel_place (url TEXT NOT NULL, alias TEXT NOT NULL, body TEXT NOT NULL, checked_at INTEGER NOT NULL,"
                        " PRIMARY KEY (url, alias))")

    def __call__(self, url: str, alias: str, country: str) -> dict | None:
        with self.db.tx() as con:
            row = con.execute("SELECT body, checked_at FROM hotel_place WHERE url = ? AND alias = ?", (url, alias)).fetchone()
        if row and (row["body"] != "null" or time.time() - row["checked_at"] < RECHECK):
            return json.loads(row["body"])
        found = self._find(url, alias, country)
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_place VALUES (?, ?, ?, ?)", (url, alias, json.dumps(found), int(time.time())))
        return found

    def _find(self, url: str, alias: str, country: str) -> dict | None:
        from .run import hotel_facts  # the facts live with the engine
        fact = next((h for h in hotel_facts() if h["url"] == url), None)
        if fact:
            return {"lat": fact["lat"], "lon": fact["lon"], "name": fact["name"], "place": fact.get("city", ""), "source": "facts"}
        _, slug = slug_words(url)
        wanted = list(words(alias)) if alias else slug
        query = alias or " ".join(slug)
        if self.sfl is not None:
            best, best_score = None, 0.0
            for h in self.sfl.suggest(query):
                if h.get("Type") not in ("hotel", "apartment") or (country and str(h.get("Country", "")).lower() != country) or not h.get("Coordinate"):
                    continue
                # the town is in the description: "Lignano Sabbiadoro, Lignano Sabbiadoro, Friuli..."
                score = similarity(wanted, f"{h.get('Text', '')} {h.get('Description', '')}")
                if score > best_score:
                    best, best_score = h, score
            if best is not None and best_score >= MATCH:
                return {"lat": float(best["Coordinate"]["lat"]), "lon": float(best["Coordinate"]["lon"]), "name": str(best.get("Text", ""))[:120],
                        "place": str(best.get("Description", "")).split(",")[0].strip()[:80], "source": "stayforlong"}
        if self.road is not None and country:
            for p in self.road.find_hotel(query, country):
                if similarity(wanted, f"{p['name']} {p['place']}") >= MATCH:
                    return {"lat": p["lat"], "lon": p["lon"], "name": p["name"], "place": p["place"], "source": "osm"}
        return None

"""Prices for the hotels a user tracks by their Booking.com link.

Booking.com gives no prices without its partner API, so each tracked hotel is
matched once to the same hotel on TripAdvisor: the hotel lists of the
regions of its country are read through Xotelo (free, cached for a week) and
the hotel is picked by the words of its Booking address, or by the current
name the user typed when Booking renamed it. The match is stored, and every
day the hotel's rates for the user's dates and party come from Xotelo: what
Booking.com, Agoda, Trip.com and others publish on TripAdvisor."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass

import httpx

from ..db import Database
from .places import words

log = logging.getLogger("weekends.tracked")

# Province-wide TripAdvisor areas where most resort hotels of a country stand,
# read first. Destination cities of the same country are read after them.
REGIONS = {
    "tr": ["g297960"],  # Antalya province: Antalya, Lara, Kemer, Belek, Side, Alanya
}
GENERIC = {"hotel", "hotels", "resort", "resorts", "spa", "the", "and", "all", "inclusive", "by", "suites", "suite",
           "beach", "club", "family", "premium", "deluxe", "de", "luxe", "golf", "turkey", "ultra"}
MAX_PAGES = 120  # 12 000 hotels per resolution at most
RECHECK_NOT_FOUND = 3 * 86400


@dataclass(frozen=True)
class Match:
    url: str
    status: str  # found, not_found
    key: str = ""
    name: str = ""
    rating: float = 0.0
    reviews: int = 0
    lat: float | None = None
    lon: float | None = None


def slug_words(url: str) -> tuple[str, list[str]]:
    """Country code and name words of a Booking hotel address."""
    m = re.search(r"/hotel/([a-z]{2})/([a-z0-9-]+)\.html$", url)
    if not m:
        return "", []
    return m.group(1), [w for w in m.group(2).split("-") if w]


def similarity(wanted: list[str], name: str) -> float:
    """Share of the wanted words found in the name; the distinctive words
    (not hotel, resort, spa and the like) count double and one of them must be
    there at all."""
    have = set(words(name))
    if not wanted:
        return 0.0
    distinctive = [w for w in wanted if w not in GENERIC and len(w) > 2]
    if distinctive and not any(w in have for w in distinctive):
        return 0.0
    weight = {w: (2.0 if w in distinctive else 1.0) for w in wanted}
    return sum(weight[w] for w in wanted if w in have) / sum(weight.values())


class Tracked:
    def __init__(self, db: Database, client: httpx.Client | None = None) -> None:
        self.db = db
        self.client = client or httpx.Client(timeout=40.0, follow_redirects=False)
        self.calls = 0
        with db.tx() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS hotel_map (
                url TEXT NOT NULL, alias TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, hotel_key TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '', rating REAL NOT NULL DEFAULT 0, reviews INTEGER NOT NULL DEFAULT 0,
                lat REAL, lon REAL, checked_at INTEGER NOT NULL, PRIMARY KEY (url, alias))""")
            con.execute("CREATE TABLE IF NOT EXISTS hotel_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    # -- cache -------------------------------------------------------------
    def _get(self, key: str, ttl: int, url: str, params: dict) -> dict | list | None:
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM hotel_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < ttl:
            return json.loads(row["body"])
        for attempt in range(3):
            try:
                self.calls += 1
                r = self.client.get(url, params=params)
                r.raise_for_status()
                value = r.json().get("result")
                break
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("xotelo failed: %s", type(exc).__name__)
                time.sleep(2 * (attempt + 1))
        else:
            return None
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(value), int(time.time())))
        return value

    def _hotels(self, geo: str, budget: list[int]):
        first = self._get(f"xlp:{geo}:0", 7 * 86400, "https://data.xotelo.com/api/list",
                          {"location_key": geo, "offset": 0, "limit": 100, "sort": "best_value"})
        if not first:
            return
        yield from first.get("list") or []
        total = int(first.get("total_count") or 0)
        for offset in range(100, total, 100):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            page = self._get(f"xlp:{geo}:{offset}", 7 * 86400, "https://data.xotelo.com/api/list",
                             {"location_key": geo, "offset": offset, "limit": 100, "sort": "best_value"})
            yield from (page or {}).get("list") or []

    # -- matching ----------------------------------------------------------
    def stored(self, url: str, alias: str = "") -> Match | None:
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM hotel_map WHERE url = ? AND alias = ?", (url, alias)).fetchone()
        if row is None:
            return None
        if row["status"] == "not_found" and time.time() - row["checked_at"] > RECHECK_NOT_FOUND:
            return None
        return Match(url=url, status=row["status"], key=row["hotel_key"], name=row["name"], rating=row["rating"],
                     reviews=row["reviews"], lat=row["lat"], lon=row["lon"])

    def resolve(self, url: str, alias: str = "", geos: list[str] | None = None) -> Match:
        known = self.stored(url, alias)
        if known:
            return known
        country, slug = slug_words(url)
        wanted = list(words(alias)) if alias else slug
        candidates = list(dict.fromkeys(REGIONS.get(country, []) + (geos or [])))
        best, best_score = None, 0.0
        budget = [MAX_PAGES]
        for geo in candidates:
            for h in self._hotels(geo, budget):
                score = similarity(wanted, h.get("name", ""))
                reviews = int((h.get("review_summary") or {}).get("count") or 0)
                if score > best_score or (score == best_score and best and reviews > int((best.get("review_summary") or {}).get("count") or 0)):
                    best, best_score = h, score
            if best_score >= 0.99:
                break
        if best is not None and best_score >= 0.75:
            summary = best.get("review_summary") or {}
            g = best.get("geo") or {}
            match = Match(url=url, status="found", key=str(best.get("key", "")), name=str(best.get("name", ""))[:120],
                          rating=float(summary.get("rating") or 0), reviews=int(summary.get("count") or 0),
                          lat=g.get("latitude"), lon=g.get("longitude"))
        else:
            match = Match(url=url, status="not_found")
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_map VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (url, alias, match.status, match.key, match.name, match.rating, match.reviews, match.lat, match.lon, int(time.time())))
        return match

    # -- prices ------------------------------------------------------------
    def price(self, key: str, checkin: str, checkout: str, guests: int, nights: int) -> tuple[float, str, float | None] | None:
        """(lowest total for the stay, whose offer it is, Booking.com's total
        when it differs). Rates are per night, so they are multiplied by the
        nights."""
        result = self._get(f"xr:{key}:{checkin}:{checkout}:{guests}", 12 * 3600, "https://data.xotelo.com/api/rates",
                           {"hotel_key": key, "chk_in": checkin, "chk_out": checkout, "adults": max(1, min(guests, 4)), "currency": "EUR"})
        rates = [r for r in (result.get("rates") or []) if r.get("rate")] if isinstance(result, dict) else []
        if not rates:
            return None
        cheapest = min(rates, key=lambda r: r["rate"])
        booking = next((r for r in rates if r.get("code") == "BookingCom"), None)
        booking_total = round(float(booking["rate"]) * nights) if booking and booking is not cheapest else None
        return round(float(cheapest["rate"]) * nights), str(cheapest.get("name", "")), booking_total


def nearest(lat: float, lon: float, airports: dict[str, dict]) -> str | None:
    best, best_km = None, 1e9
    for code, d in airports.items():
        if d.get("lat") is None:
            continue
        p1, p2 = math.radians(lat), math.radians(d["lat"])
        dl = math.radians(d["lon"] - lon)
        km = 6371 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))
        if km < best_km:
            best, best_km = code, km
    return best if best_km < 250 else None

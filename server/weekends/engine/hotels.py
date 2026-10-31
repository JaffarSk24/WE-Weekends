"""Where a stay comes from, best source first:

1. Booking.com Demand API, as soon as BOOKING_API_TOKEN and
   BOOKING_AFFILIATE_ID are present in the environment (switches by itself).
2. Xotelo (free, no key): rates on the given dates that Booking.com, Agoda,
   Trip.com and others publish through TripAdvisor, for cities with a known
   TripAdvisor location key (data/tripadvisor.json).
3. A Booking.com search link with the dates, the party and the filters, so
   the user sees real prices there.

Every result keeps a direct link; nothing is ever booked by us."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..db import Database

log = logging.getLogger("weekends.hotels")
DATA = Path(__file__).resolve().parents[1] / "data"


@dataclass(frozen=True)
class Stay:
    name: str
    price: float | None  # total for all nights and the whole party, EUR
    rating: float | None  # on a 10-point scale
    reviews: int | None
    url: str
    source: str  # booking, xotelo, link
    approx: bool = False


@dataclass(frozen=True)
class StayQuery:
    city: str
    country: str
    iata: str
    lat: float | None
    lon: float | None
    checkin: date
    checkout: date
    adults: int
    children: tuple[int, ...]
    min_rating: float
    min_reviews: int
    breakfast: str
    min_stars: int
    lang: str

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days


def booking_search_link(q: StayQuery) -> str:
    filters = []
    if q.min_rating >= 6:
        filters.append(f"review_score={int(q.min_rating * 10)}")
    if q.min_stars:
        filters.append(";".join(f"class={s}" for s in range(q.min_stars, 6)))
    if q.breakfast in ("prefer", "require"):
        filters.append("mealplan=1")
    params = [
        ("ss", f"{q.city}"), ("checkin", q.checkin.isoformat()), ("checkout", q.checkout.isoformat()),
        ("group_adults", q.adults), ("group_children", len(q.children)), ("no_rooms", 1),
        ("lang", {"ru": "ru", "sk": "sk", "en": "en-gb"}[q.lang]), ("selected_currency", "EUR"), ("order", "price"),
    ]
    params += [("age", a) for a in q.children]
    if filters:
        params.append(("nflt", ";".join(filters)))
    return "https://www.booking.com/searchresults.html?" + urlencode(params)


def without_affiliate(url: str) -> str:
    """WE Weekends takes no commissions: partner parameters are stripped from
    every link before it reaches a user."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in ("aid", "label", "sid", "affiliate_id")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@lru_cache(maxsize=1)
def tripadvisor_keys() -> dict[str, str]:
    path = DATA / "tripadvisor.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class Hotels:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.client = httpx.Client(timeout=25.0, follow_redirects=False)
        self.booking_token = os.environ.get("BOOKING_API_TOKEN", "").strip()
        self.booking_affiliate = os.environ.get("BOOKING_AFFILIATE_ID", "").strip()
        self.calls = 0
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS hotel_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    @property
    def source(self) -> str:
        if self.booking_token and self.booking_affiliate:
            return "booking"
        return "xotelo"

    def cheapest(self, q: StayQuery) -> Stay:
        link = booking_search_link(q)
        stay = None
        try:
            if self.source == "booking":
                stay = self._booking(q)
            elif q.iata in tripadvisor_keys():
                stay = self._xotelo(q, tripadvisor_keys()[q.iata])
        except Exception:  # a broken source must never break the digest
            log.exception("stay lookup failed for %s", q.iata)
        return stay or Stay(name="", price=None, rating=None, reviews=None, url=link, source="link")

    # -- cache -----------------------------------------------------------
    def _cached(self, key: str, ttl: int, fetch) -> object:
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM hotel_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < ttl:
            return json.loads(row["body"])
        self.calls += 1
        value = fetch()
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(value), int(time.time())))
        return value

    # -- Xotelo ----------------------------------------------------------
    def _xotelo(self, q: StayQuery, location: str) -> Stay | None:
        def fetch_list():
            r = self.client.get("https://data.xotelo.com/api/list", params={"location_key": location, "offset": 0, "limit": 30, "sort": "best_value"})
            r.raise_for_status()
            return (r.json().get("result") or {}).get("list") or []

        hotels = self._cached(f"xl:{location}", 7 * 86400, fetch_list)
        good = []
        for h in hotels:
            summary = h.get("review_summary") or {}
            rating = float(summary.get("rating") or 0) * 2  # TripAdvisor rates out of 5
            count = int(summary.get("count") or 0)
            low = (h.get("price_ranges") or {}).get("minimum")
            if rating >= q.min_rating and count >= q.min_reviews and low and h.get("accommodation_type") in ("Hotel", "Specialty lodging", "B&B", "Inn", None):
                good.append((low, rating, count, h))
        good.sort(key=lambda x: x[0])
        persons = q.adults + len(q.children)
        best: Stay | None = None
        for _, rating, count, h in good[:4]:
            def fetch_rates(h=h):
                r = self.client.get("https://data.xotelo.com/api/rates", params={
                    "hotel_key": h["key"], "chk_in": q.checkin.isoformat(), "chk_out": q.checkout.isoformat(),
                    "adults": min(persons, 4), "currency": "EUR"})
                r.raise_for_status()
                return (r.json().get("result") or {}).get("rates") or []

            rates = self._cached(f"xr:{h['key']}:{q.checkin}:{q.checkout}:{persons}", 12 * 3600, fetch_rates)
            prices = [float(x["rate"]) for x in rates if x.get("rate")]
            booking = [float(x["rate"]) for x in rates if x.get("code") == "BookingCom" and x.get("rate")]
            if not prices:
                continue
            nightly = booking[0] if booking else min(prices)
            total = round(nightly * q.nights)
            if best is None or total < (best.price or 1e9):
                best = Stay(name=h.get("name", ""), price=total, rating=round(rating, 1), reviews=count,
                            url=booking_search_link(q) if booking else str(h.get("url", "")), source="xotelo",
                            approx=len(q.children) > 0 or persons > 4)
        return best

    # -- Booking.com Demand API ------------------------------------------
    def _booking(self, q: StayQuery) -> Stay | None:
        """Demand API v3.1 accommodations/search around the destination, cheapest
        first. Enabled only with credentials; cached per query."""
        def fetch():
            body = {
                "booker": {"country": "sk", "platform": "desktop"},
                "checkin": q.checkin.isoformat(), "checkout": q.checkout.isoformat(),
                "coordinates": {"latitude": q.lat, "longitude": q.lon, "radius": 25} if q.lat is not None else None,
                "guests": {"number_of_adults": q.adults, "number_of_rooms": 1, **({"children": list(q.children)} if q.children else {})},
                "currency": "EUR", "extras": ["extra_charges", "products"], "rows": 50,
                "sort": {"by": "price", "direction": "ascending"},
            }
            if body["coordinates"] is None:
                return []
            r = self.client.post(
                "https://demandapi.booking.com/3.1/accommodations/search",
                json={k: v for k, v in body.items() if v is not None},
                headers={"Authorization": f"Bearer {self.booking_token}", "X-Affiliate-Id": self.booking_affiliate},
            )
            r.raise_for_status()
            return r.json().get("data") or []

        rows = self._cached(f"bk:{q.city}:{q.country}:{q.checkin}:{q.checkout}:{q.adults}:{q.children}", 6 * 3600, fetch)
        for row in rows:
            rating = (row.get("review") or {}).get("score")
            reviews = (row.get("review") or {}).get("number_of_reviews")
            price = (row.get("price") or {}).get("book")
            if price and (rating or 0) >= q.min_rating and (reviews or 0) >= q.min_reviews:
                return Stay(name=row.get("name", ""), price=float(price), rating=rating, reviews=reviews,
                            url=without_affiliate(row.get("url") or booking_search_link(q)), source="booking")
        return None

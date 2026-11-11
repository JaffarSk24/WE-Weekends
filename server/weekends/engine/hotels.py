"""Where a stay comes from, best source first:

1. Booking.com Demand API, as soon as BOOKING_API_TOKEN and
   BOOKING_AFFILIATE_ID are present in the environment (switches by itself).
2. Xotelo (free, no key): rates on the given dates that Booking.com, Agoda,
   Trip.com and others publish through TripAdvisor, for cities with a known
   TripAdvisor location key (data/tripadvisor.json).
3. Stayforlong (free, no key): its hotels around the destination with the
   total for the dates and the party, when Xotelo has no rates.
4. An estimate from the usual nightly price of the city
   (data/stay_baseline.json), marked as an estimate, so a trip always has a
   total. The link is a Booking.com search with the dates, the party and the
   filters, where the user sees the real prices.

Every result keeps a direct link; nothing is ever booked by us."""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..db import Database
from .places import _contains, place_of, words

log = logging.getLogger("weekends.hotels")
SEEN_DAYS = 90  # how long a found price counts for the estimates
MAX_RATE_LOOKUPS = 6  # hotels of one city asked for rates, at the same time
DATA = Path(__file__).resolve().parents[1] / "data"


@dataclass(frozen=True)
class Stay:
    name: str
    price: float | None  # total for all nights and the whole party, EUR
    rating: float | None  # on a 10-point scale
    reviews: int | None
    url: str
    source: str  # booking, xotelo, stayforlong, estimate, link
    place: str | None = None  # known resort or city the hotel is in
    approx: bool = False
    seller: str = ""  # whose price it is: Booking.com, Agoda.com, Vio.com...


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
    exclude: tuple[tuple[str, ...], ...] = ()  # word patterns of excluded places
    avoid: tuple[str, ...] = ()  # hotel names the user marked down

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days


def booking_search_link(q: StayQuery, hotel: str = "") -> str:
    """A Booking.com search for the exact dates and party; with a hotel name it
    opens straight on that hotel."""
    filters = []
    if q.min_rating >= 6:
        filters.append(f"review_score={int(q.min_rating * 10)}")
    if q.min_stars:
        filters.append(";".join(f"class={s}" for s in range(q.min_stars, 6)))
    if q.breakfast in ("prefer", "require"):
        filters.append("mealplan=1")
    params = [
        ("ss", f"{hotel}, {q.city}" if hotel else q.city), ("checkin", q.checkin.isoformat()), ("checkout", q.checkout.isoformat()),
        ("group_adults", q.adults), ("group_children", len(q.children)), ("no_rooms", 1),
        ("lang", {"ru": "ru", "sk": "sk", "en": "en-gb"}[q.lang]), ("selected_currency", "EUR"), ("order", "price"),
    ]
    params += [("age", a) for a in q.children]
    if filters and not hotel:
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


@lru_cache(maxsize=1)
def stay_baseline() -> dict:
    path = DATA / "stay_baseline.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def party_factor(persons: int) -> float:
    """A single traveller pays most of a double room, and each further guest
    adds about a third."""
    return 0.85 if persons == 1 else 1 + 0.35 * max(0, persons - 2)


def estimate(q: StayQuery, seen: float | None = None) -> float | None:
    """The usual price of a good hotel room in the city for these nights:
    what the engine itself paid attention to lately (seen, a night for two),
    else the city's nightly price for two from the baseline, else the
    country's, else the middle of all cities."""
    base = stay_baseline()
    nightly = seen or (base.get("cities") or {}).get(q.iata) or (base.get("countries") or {}).get(q.country.upper()) or base.get("all")
    if not nightly:
        return None
    factor = party_factor(q.adults + len(q.children))
    nightly = max(30.0, min(float(nightly), 350.0))  # one odd weekend must not make a city free or unaffordable
    return round(nightly * factor * q.nights)


class Hotels:
    def __init__(self, db: Database, stayforlong=None) -> None:
        self.db = db
        self.stayforlong = stayforlong
        self.client = httpx.Client(timeout=25.0, follow_redirects=False)
        self.booking_token = os.environ.get("BOOKING_API_TOKEN", "").strip()
        self.booking_affiliate = os.environ.get("BOOKING_AFFILIATE_ID", "").strip()
        self.calls = 0
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS hotel_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")
            # Real prices found, a night for two: the estimates learn from them.
            con.execute("CREATE TABLE IF NOT EXISTS stay_seen (iata TEXT NOT NULL, seen_at INTEGER NOT NULL, nightly REAL NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS stay_seen_iata ON stay_seen (iata, seen_at)")

    def _remember(self, q: StayQuery, stay: Stay) -> None:
        if not stay.price or q.nights < 1:
            return
        nightly = stay.price / q.nights / party_factor(q.adults + len(q.children))
        with self.db.tx() as con:
            con.execute("INSERT INTO stay_seen VALUES (?, ?, ?)", (q.iata, int(time.time()), nightly))
            con.execute("DELETE FROM stay_seen WHERE seen_at < ?", (int(time.time()) - SEEN_DAYS * 86400,))

    def _seen(self, iata: str) -> float | None:
        """The middle of the real nightly prices of the last months, once
        there are a few of them."""
        with self.db.tx() as con:
            rows = con.execute("SELECT nightly FROM stay_seen WHERE iata = ? AND seen_at >= ?", (iata, int(time.time()) - SEEN_DAYS * 86400)).fetchall()
        return statistics.median(r["nightly"] for r in rows) if len(rows) >= 3 else None

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
        if stay is None and self.stayforlong is not None and q.lat is not None:
            try:
                stay = self.stayforlong.around(q)
            except Exception:
                log.exception("stayforlong lookup failed for %s", q.iata)
        if stay is not None:
            self._remember(q, stay)
        else:
            guess = estimate(q, self._seen(q.iata))
            if guess:
                return Stay(name="", price=guess, rating=None, reviews=None, url=link, source="estimate", approx=True)
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
            if rating >= q.min_rating and count >= q.min_reviews and h.get("accommodation_type") in ("Hotel", "Specialty lodging", "B&B", "Inn", None):
                location = str(h.get("url", "")).rsplit("-Reviews-", 1)[-1].removesuffix(".html")
                tokens = words(location.replace("-", " ").replace("_", " "))
                if any(_contains(tokens, pattern) for pattern in q.exclude) or h.get("name", "") in q.avoid:
                    continue
                good.append((rating, count, h))
        # The list no longer says what a hotel costs, so the first ones in its
        # own best-value order are asked for rates, all at once: one answer
        # takes three to seven seconds.
        persons = q.adults + len(q.children)

        def rates_of(h: dict) -> list:
            def fetch_rates():
                r = self.client.get("https://data.xotelo.com/api/rates", params={
                    "hotel_key": h["key"], "chk_in": q.checkin.isoformat(), "chk_out": q.checkout.isoformat(),
                    "adults": min(persons, 4), "currency": "EUR"})
                r.raise_for_status()
                return (r.json().get("result") or {}).get("rates") or []

            try:
                return self._cached(f"xw:{h['key']}:{q.checkin}:{q.checkout}:{persons}", 12 * 3600, fetch_rates)
            except (httpx.HTTPError, ValueError):
                return []

        asked = good[:MAX_RATE_LOOKUPS]
        with ThreadPoolExecutor(max_workers=MAX_RATE_LOOKUPS) as pool:
            answers = list(pool.map(rates_of, [h for _, _, h in asked]))
        best: Stay | None = None
        for (rating, count, h), rates in zip(asked, answers, strict=True):
            offers = [x for x in rates if x.get("rate")]
            if not offers:
                continue
            offer = min(offers, key=lambda x: float(x["rate"]))  # the lowest price, with the name of who sells at it
            total = round(float(offer["rate"]) * q.nights)
            if best is None or total < (best.price or 1e9):
                location = str(h.get("url", "")).rsplit("-Reviews-", 1)[-1].removesuffix(".html").split("-", 1)[-1]
                best = Stay(name=h.get("name", ""), price=total, rating=round(rating, 1), reviews=count, place=place_of(location.replace("_", " ")),
                            url=booking_search_link(q, h.get("name", "")), source="xotelo", seller=str(offer.get("name") or "")[:40],
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

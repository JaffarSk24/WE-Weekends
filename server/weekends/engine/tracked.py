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
MAX_KM = 3.0  # a TripAdvisor hotel further than this from the Booking one is another hotel
NEAR_KM = 15.0  # the towns around a hotel, whose TripAdvisor lists are read
COUNTRY_PAGES = 40
NAME_SCORE = 0.95  # without the place only a near-exact name is trusted
NEAR_SCORE = 0.6  # with it, a name shared in part will do
# Country-wide TripAdvisor areas: their lists tell the town areas around a hotel.
TA_COUNTRIES = {"tr": "g293969", "gr": "g189398", "cy": "g190372", "eg": "g294200", "es": "g187427", "bg": "g294451", "tn": "g293753",
                "it": "g187768", "hr": "g294453", "al": "g294445", "mt": "g190311", "pt": "g189100", "ae": "g295424", "ma": "g293730",
                "fr": "g187070", "at": "g190410", "de": "g187275", "si": "g274862", "cz": "g274684", "hu": "g274881", "sk": "g274922",
                "pl": "g274723", "ro": "g294457", "ge": "g294194", "am": "g293931", "rs": "g294471", "ba": "g294449", "xk": "g295385",
                "mk": "g295109", "il": "g293977", "jo": "g293985"}
RECHECK_NOT_FOUND = 3 * 86400


@dataclass(frozen=True)
class Match:
    url: str
    status: str  # found (on TripAdvisor), located (only its place is known), not_found
    key: str = ""
    name: str = ""
    rating: float = 0.0
    reviews: int = 0
    lat: float | None = None
    lon: float | None = None
    place: str = ""  # the town it stands in
    ta_url: str = ""  # its TripAdvisor page, to check the match by


def ta_key(url: str) -> str | None:
    """The TripAdvisor hotel key (g<area>-d<hotel>) of a TripAdvisor address."""
    m = re.search(r"tripadvisor\.com/Hotel_Review-(g\d+-d\d+)-Reviews-", url)
    return m.group(1) if m else None


def slug_words(url: str) -> tuple[str, list[str]]:
    """Country code and name words of a Booking hotel address; of a
    TripAdvisor one the name words only (it names no country)."""
    m = re.search(r"/hotel/([a-z]{2})/([a-z0-9-]+)\.html$", url)
    if m:
        return m.group(1), [w for w in m.group(2).split("-") if w]
    t = re.search(r"-Reviews-([^-]+)-", url)
    return "", list(words(t.group(1).replace("_", " "))) if t else []


def hotel_name(url: str) -> str:
    """A readable name from the address, before the hotel is looked up."""
    t = re.search(r"-Reviews-([^-]+)-", url)
    if t:
        return t.group(1).replace("_", " ")
    return url.rsplit("/", 1)[-1].removesuffix(".html").replace("-", " ").title()


def hotel_country(m) -> str:
    """The hotel's country code: in a Booking address, else where the found
    hotel stands."""
    found = re.search(r"/hotel/([a-z]{2})/", m.url)
    if found:
        return found.group(1)
    if m.lat is not None:
        from .road import country_at
        return (country_at(m.lat, m.lon) or "").lower()
    return ""


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
    def __init__(self, db: Database, client: httpx.Client | None = None, locate=None) -> None:
        """locate(url, alias, country) says where the Booking hotel really
        stands: {lat, lon, name, place}, or None."""
        self.db = db
        self.client = client or httpx.Client(timeout=40.0, follow_redirects=False)
        self.locate = locate
        self.calls = 0
        self._near: dict[str, list[tuple[float, float, str]]] = {}
        with db.tx() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS hotel_map (
                url TEXT NOT NULL, alias TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, hotel_key TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '', rating REAL NOT NULL DEFAULT 0, reviews INTEGER NOT NULL DEFAULT 0,
                lat REAL, lon REAL, checked_at INTEGER NOT NULL, PRIMARY KEY (url, alias))""")
            columns = {r["name"] for r in con.execute("PRAGMA table_info(hotel_map)")}
            if "place" not in columns:
                con.execute("ALTER TABLE hotel_map ADD COLUMN place TEXT NOT NULL DEFAULT ''")
                con.execute("ALTER TABLE hotel_map ADD COLUMN ta_url TEXT NOT NULL DEFAULT ''")
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
                     reviews=row["reviews"], lat=row["lat"], lon=row["lon"], place=row["place"], ta_url=row["ta_url"])

    def near_geos(self, country: str, spot: tuple[float, float]) -> list[str]:
        """The TripAdvisor areas of the towns around a place: learned from the
        country-wide list (its best 3000 hotels or so), the areas of the
        hotels within NEAR_KM counted, the most common first."""
        if country not in TA_COUNTRIES:
            return []
        if country not in self._near:
            points = []
            for item in self._hotels(TA_COUNTRIES[country], [COUNTRY_PAGES]):
                g = item.get("geo") or {}
                if g.get("latitude") is not None and "-" in str(item.get("key", "")):
                    points.append((float(g["latitude"]), float(g["longitude"]), str(item["key"]).split("-")[0]))
            self._near[country] = points
        counts: dict[str, int] = {}
        for lat, lon, geo in self._near[country]:
            if km(spot, (lat, lon)) <= NEAR_KM:
                counts[geo] = counts.get(geo, 0) + 1
        return sorted(counts, key=lambda g: -counts[g])[:4]

    def _from_tripadvisor(self, url: str, alias: str) -> Match:
        """A hotel given by its TripAdvisor address needs no matching: its key
        is in the address. Its name, rating and place come from its area's
        list; a hotel too far down the list keeps the name from the address."""
        key = ta_key(url)
        geo = key.split("-")[0]
        found = None
        for h in self._hotels(geo, [MAX_PAGES]):
            if str(h.get("key", "")) == key:
                found = h
                break
        summary = (found or {}).get("review_summary") or {}
        g = (found or {}).get("geo") or {}
        match = Match(url=url, status="found", key=key, name=str((found or {}).get("name") or hotel_name(url))[:120],
                      rating=float(summary.get("rating") or 0), reviews=int(summary.get("count") or 0),
                      lat=g.get("latitude"), lon=g.get("longitude"), place="", ta_url=url)
        if match.lat is None and self.locate is not None:  # not in the list: the place by its name
            try:
                spot = self.locate(url, alias or match.name, "")
            except Exception:
                spot = None
            if spot:
                match = Match(**{**match.__dict__, "lat": spot["lat"], "lon": spot["lon"], "place": spot.get("place", "")})
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_map (url, alias, status, hotel_key, name, rating, reviews, lat, lon, checked_at, place, ta_url)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (url, alias, match.status, match.key, match.name, match.rating, match.reviews, match.lat, match.lon, int(time.time()),
                         match.place, match.ta_url))
        return match

    def resolve(self, url: str, alias: str = "", geos: list[str] | None = None) -> Match:
        """The tracked hotel on TripAdvisor. Its real place comes first
        (locate), and only a hotel within MAX_KM of it counts as the same: a
        name alone matched hotels of the same name in other towns. When
        TripAdvisor has no such hotel, the place is kept (located), so prices
        from Stayforlong and the nearest airport still work."""
        known = self.stored(url, alias)
        if known:
            return known
        if ta_key(url):
            return self._from_tripadvisor(url, alias)
        country, slug = slug_words(url)
        spot = None
        if self.locate is not None:
            try:
                spot = self.locate(url, alias, country)
            except Exception:  # a broken locator must not stop the matching
                log.exception("locating the hotel failed")
        here = (spot["lat"], spot["lon"]) if spot else None
        wanted = list(words(alias)) if alias else (list(words(spot["name"])) if spot and spot.get("name") else slug)
        candidates = list(dict.fromkeys((self.near_geos(country, here) if here else []) + REGIONS.get(country, []) + (geos or [])))
        best, best_score, best_km = None, 0.0, 1e9
        budget = [MAX_PAGES]
        for geo in candidates:
            for h in self._hotels(geo, budget):
                g = h.get("geo") or {}
                distance = 0.0
                if here:
                    if g.get("latitude") is None:
                        continue
                    distance = km(here, (float(g["latitude"]), float(g["longitude"])))
                    if distance > MAX_KM:
                        continue
                score = similarity(wanted, h.get("name", ""))
                if score > best_score or (score == best_score and best is not None and distance < best_km):
                    best, best_score, best_km = h, score, distance
            if best_score >= 0.99:
                break
        if best is not None and best_score >= (NEAR_SCORE if here else NAME_SCORE):
            summary = best.get("review_summary") or {}
            g = best.get("geo") or {}
            match = Match(url=url, status="found", key=str(best.get("key", "")), name=str(best.get("name", ""))[:120],
                          rating=float(summary.get("rating") or 0), reviews=int(summary.get("count") or 0),
                          lat=here[0] if here else g.get("latitude"), lon=here[1] if here else g.get("longitude"),
                          place=(spot or {}).get("place", "")[:80], ta_url=str(best.get("url", ""))[:300])
        elif spot:
            match = Match(url=url, status="located", name=str(spot.get("name", ""))[:120], lat=here[0], lon=here[1], place=spot.get("place", "")[:80])
        else:
            match = Match(url=url, status="not_found")
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO hotel_map (url, alias, status, hotel_key, name, rating, reviews, lat, lon, checked_at, place, ta_url)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (url, alias, match.status, match.key, match.name, match.rating, match.reviews, match.lat, match.lon, int(time.time()),
                         match.place, match.ta_url))
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


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


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

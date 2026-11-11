"""Prices from Stayforlong, shown next to the TripAdvisor ones.

Stayforlong sells long hotel stays and often undercuts what TripAdvisor
lists. Its site reads prices from a public gateway: the hotel search the page
uses (/v2/suggester) and the rooms with their rates for given dates and party
(/v4/availability/property/<id>). Each rate says what is paid now, until when
it can be cancelled for free and the board, so free cancellation and paying
later can be asked for. The engine asks politely: one hotel at a time with a
pause, under its own name, and every answer is cached for half a day for all
users with the same dates and party."""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass

import httpx

from ..db import Database
from .places import _contains, place_of, words
from .tracked import similarity

log = logging.getLogger("weekends.stayforlong")

GATEWAY = "https://gw.stayforlong.com"
SITE = "https://www.stayforlong.com"
AGENT = "WE-Weekends/1.0 (+https://github.com/JaffarSk24/WE-Weekends)"
MAX_KM = 2.0  # a search hit counts as the same hotel only this close
PAUSE = 1.0  # seconds between live requests
PRICES_TTL = 12 * 3600
RECHECK_NOT_FOUND = 7 * 86400
ALL_INCLUSIVE = {"AI", "AS", "UAI"}
AROUND_LAT, AROUND_LON = 0.13, 0.2  # about 15 km each way around a city
MIN_REVIEWS = 30


@dataclass(frozen=True)
class Offer:
    total: int  # the whole stay, taxes and fees included
    board: str  # board group code: AI, RO, BB, HB, FB...
    policy: str  # NOR: free cancellation, paid later; SFL: deposit now; NRF: paid now, no refund
    pay_now: int
    until: str  # ISO date: the last day of free cancellation, or when the rest is paid
    url: str

    @property
    def free_cancellation(self) -> bool:
        return self.policy == "NOR" and self.pay_now == 0

    @property
    def pay_later(self) -> bool:
        return self.pay_now < self.total


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def hotel_path(country: str, slug: str, city: str) -> str:
    """The hotel page path, as the site builds it: tr/<slug>_<city>."""
    return f"{country}/{slug}_{city}"


def page_url(path: str, lang: str, checkin: str, checkout: str, adults: int, ages: tuple[int, ...]) -> str:
    market = "en-gb" if lang == "en" else "sk-sk"  # there is no Russian site, the Slovak one opens instead
    q = f"?adults={adults}&checkIn={checkin}&checkOut={checkout}"
    if ages:
        q += "&children=" + ",".join(str(a) for a in ages)
    return f"{SITE}/{market}/hotel/{path}{q}"


class Stayforlong:
    def __init__(self, db: Database, client: httpx.Client | None = None) -> None:
        self.db = db
        self.client = client or httpx.Client(timeout=40.0, follow_redirects=False, headers={"User-Agent": AGENT, "Accept": "application/json"})
        self.calls = 0
        self._last = 0.0
        with db.tx() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS sfl_map (
                url TEXT PRIMARY KEY, status TEXT NOT NULL, sfl_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL)""")
            con.execute("CREATE TABLE IF NOT EXISTS sfl_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")
            if "lat" not in {r["name"] for r in con.execute("PRAGMA table_info(sfl_map)")}:
                con.execute("ALTER TABLE sfl_map ADD COLUMN lat REAL")
                con.execute("ALTER TABLE sfl_map ADD COLUMN lon REAL")
        self.positions: dict[str, tuple[float, float]] = {}  # where the hotels just matched stand

    def _request(self, method: str, url: str, **kw) -> object | None:
        wait = PAUSE - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(2):
            try:
                self.calls += 1
                r = self.client.request(method, url, **kw)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("stayforlong failed: %s", type(exc).__name__)
                time.sleep(3 * (attempt + 1))
            finally:
                self._last = time.monotonic()
        return None

    # -- matching ----------------------------------------------------------
    def search(self, name: str, lat: float | None, lon: float | None, country: str) -> tuple[str, str] | None:
        """(Stayforlong id, page path) of the hotel with this name: the hit
        in the same country whose name matches best, standing within 2 km.
        Without coordinates only a near-exact name does."""
        hits = self._request("POST", f"{GATEWAY}/v2/suggester",
                             json={"affiliate": "stayforlong", "lang": "en", "market": "sk", "keyword": name[:100]})
        wanted = list(words(name))
        best, best_score = None, 0.0
        for h in hits if isinstance(hits, list) else []:
            if h.get("Type") not in ("hotel", "apartment") or str(h.get("Country", "")).lower() != country:
                continue
            if not (h.get("ID") and h.get("Slug") and h.get("CitySlug")):
                continue
            score = similarity(wanted, str(h.get("Text", "")))
            c = h.get("Coordinate") or {}
            if lat is not None and lon is not None and c.get("lat") is not None:
                if score < 0.75 or _km((lat, lon), (c["lat"], c["lon"])) > MAX_KM:
                    continue
            elif score < 0.95:
                continue
            if score > best_score:
                best, best_score = h, score
        if best is None:
            return None
        c = best.get("Coordinate") or {}
        if c.get("lat") is not None:
            self.positions[str(best["ID"])] = (float(c["lat"]), float(c["lon"]))
        return str(best["ID"]), hotel_path(country, str(best["Slug"]), str(best["CitySlug"]))

    def resolve(self, url: str, name: str, lat: float | None, lon: float | None, country: str) -> tuple[str, str] | None:
        """search(), remembered per Booking address."""
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM sfl_map WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "found" and row["lat"] is None and lat is None and time.time() - row["checked_at"] > RECHECK_NOT_FOUND:
            row = None  # matched before positions were kept: looked up once more
        if row and (row["status"] == "found" or time.time() - row["checked_at"] < RECHECK_NOT_FOUND):
            return (row["sfl_id"], row["path"]) if row["status"] == "found" else None
        found = self.search(name, lat, lon, country) if name else None
        lat_lon = self.positions.get(found[0]) if found else None
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sfl_map (url, status, sfl_id, path, checked_at, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (url, "found" if found else "not_found", found[0] if found else "", found[1] if found else "", int(time.time()),
                         lat_lon[0] if lat_lon else None, lat_lon[1] if lat_lon else None))
        return found

    def position(self, url: str) -> tuple[float, float] | None:
        """Where a matched hotel stands, for the nearest airport when
        TripAdvisor does not know the hotel."""
        with self.db.tx() as con:
            row = con.execute("SELECT lat, lon FROM sfl_map WHERE url = ? AND status = 'found'", (url,)).fetchone()
        return (row["lat"], row["lon"]) if row and row["lat"] is not None else None

    # -- prices ------------------------------------------------------------
    def offers(self, sfl_id: str, checkin: str, checkout: str, adults: int, ages: tuple[int, ...]) -> list[dict]:
        """Every rate of the hotel for the stay, reduced to what is used."""
        key = f"sfl:{sfl_id}:{checkin}:{checkout}:{adults}:{','.join(map(str, ages))}"
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM sfl_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < PRICES_TTL:
            return json.loads(row["body"])
        params = {"adultsByRoom": adults, "checkin": checkin, "checkout": checkout, "lang": "en", "language": "en", "market": "sk",
                  "currency": "EUR", "device": "desktop", "affiliate": "stayforlong", "source": "web",
                  "childrenByRoom": len(ages)}  # without it the gateway answers with the hotel description, not its rates
        if ages:
            params["childrenAges"] = ",".join(map(str, ages))
        data = self._request("GET", f"{GATEWAY}/v4/availability/property/{sfl_id}", params=params)
        if not isinstance(data, dict) or sfl_id not in data:
            if data is not None:
                log.warning("stayforlong answered without rates for %s", sfl_id)
            return []  # not cached: tried again next time
        rates = []
        for a in (data.get(sfl_id) or {}).get("availability") or []:
            total = ((a.get("summary") or {}).get("total_with_fees") or {}).get("amount")
            if not total or a.get("is_prime") or a.get("currency", "EUR") != "EUR" or int(a.get("rooms") or 1) != 1:
                continue  # Prime prices need a paid membership
            dates = sorted(str(p.get("from", "")) for p in a.get("cancellation_policies") or [] if p.get("from"))
            rates.append({"total": round(float(total)), "board": str(a.get("board_group_code") or a.get("board_code") or ""),
                          "policy": str(a.get("payment_policy") or ""), "pay_now": round(float(a.get("pay_rate") or 0)),
                          "until": dates[0] if dates else ""})
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sfl_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(rates), int(time.time())))
        return rates

    def best(self, sfl_id: str, path: str, lang: str, checkin: str, checkout: str, adults: int, ages: tuple[int, ...],
             all_inclusive: bool = False, free_cancellation: bool = False, pay_later: bool = False) -> Offer | None:
        """The cheapest rate meeting the user's conditions. All inclusive is
        kept to when the hotel sells it at all."""
        url = page_url(path, lang, checkin, checkout, adults, ages)
        offers = [Offer(url=url, **r) for r in self.offers(sfl_id, checkin, checkout, adults, ages)]
        if all_inclusive and any(o.board in ALL_INCLUSIVE for o in offers):
            offers = [o for o in offers if o.board in ALL_INCLUSIVE]
        offers = [o for o in offers if (not free_cancellation or o.free_cancellation) and (not pay_later or o.pay_later)]
        return min(offers, key=lambda o: o.total) if offers else None

    # -- hotels around a city ----------------------------------------------
    def around(self, q):
        """The cheapest hotel around the destination that meets the user's
        stay rules, for a weekend trip: a search of the box around the city,
        cheapest first, and the next fifteen hotels when none of the first
        ones will do. Hostels and dormitory beds are left out."""
        from .hotels import Stay
        need = min(q.min_reviews, MIN_REVIEWS)  # Stayforlong counts its own guests, far fewer than TripAdvisor
        seen: list[int] = []
        for batch in range(2):
            found = self._around(q, batch, seen)
            if found is None:
                return None
            ok = [h for h in found if h["rating"] >= q.min_rating and h["reviews"] >= need and h["name"] not in q.avoid
                  and not any(_contains(words(f"{h['name']} {h['city']}"), pattern) for pattern in q.exclude)]
            if ok:
                h = min(ok, key=lambda x: x["total"])
                return Stay(name=h["name"], price=h["total"], rating=h["rating"], reviews=h["reviews"], place=place_of(h["city"]),
                            source="stayforlong", seller="Stayforlong",
                            url=page_url(h["path"], q.lang, q.checkin.isoformat(), q.checkout.isoformat(), q.adults, q.children))
            seen += [h["id"] for h in found if h.get("id")]
            if len(found) < 10:
                break  # the city has no more
        return None

    def _around(self, q, batch: int, seen: list[int]) -> list[dict] | None:
        """One batch of the search: the hotels not seen yet, as the site
        itself loads more of them."""
        key = f"sfa:{q.iata}:{q.checkin}:{q.checkout}:{q.adults}:{','.join(map(str, q.children))}:{q.min_rating}:{q.min_stars}:{batch}"
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM sfl_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < PRICES_TTL:
            return json.loads(row["body"])
        body = {"affiliate": "stayforlong", "source": "web", "offer_type": "multi-offer", "device": "desktop",
                "checkin": q.checkin.isoformat(), "checkout": q.checkout.isoformat(), "lang": "en", "market": "sk", "currency": "EUR",
                "rooms": [{"adults": q.adults, "children": list(q.children)}], "sortBy": "price", "page": 0, "offset": 0,
                "latitude": q.lat, "longitude": q.lon, "accommodations": ["HOTEL"],
                "box_shape": {"top_left": {"lon": q.lon - AROUND_LON, "lat": q.lat + AROUND_LAT},
                              "bottom_right": {"lon": q.lon + AROUND_LON, "lat": q.lat - AROUND_LAT}}}
        if seen:
            body["excludedIDs"] = seen
        if q.min_rating >= 6:
            body["rating"] = int(q.min_rating)
        if q.min_stars:
            body["stars"] = list(range(q.min_stars, 6))
        data = self._request("POST", f"{GATEWAY}/v3/search-by-coordinates-geo", json=body)
        if not isinstance(data, dict) or "properties" not in data:
            return None  # not cached: tried again next time
        found = []
        for h in data.get("properties") or []:
            room = ((h.get("rooms") or [{}])[0].get("room_name") or "").lower()
            loc = h.get("property_location") or {}
            entry = {"id": h.get("id"), "name": str(h.get("name", ""))[:120], "total": 0, "rating": 0.0, "reviews": 0, "city": str(loc.get("city", "")), "path": ""}
            if h.get("currency", "EUR") == "EUR" and h.get("selling_rate") and not any(w in room for w in ("dorm", "bed in", "shared")):
                entry |= {"total": round(float(h["selling_rate"])), "rating": round(float(h.get("rating") or 0), 1), "reviews": int(h.get("comments") or 0),
                          "path": hotel_path(str(loc.get("country_code", "")).lower(), str(h.get("slug", "")), str(loc.get("city_slug", "")))}
            found.append(entry)  # the unfit ones too: their ids are left out of the next batch
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sfl_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(found), int(time.time())))
        return found

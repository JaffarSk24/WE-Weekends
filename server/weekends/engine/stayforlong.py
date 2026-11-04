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
from .places import words
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
        return str(best["ID"]), hotel_path(country, str(best["Slug"]), str(best["CitySlug"]))

    def resolve(self, url: str, name: str, lat: float | None, lon: float | None, country: str) -> tuple[str, str] | None:
        """search(), remembered per Booking address."""
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM sfl_map WHERE url = ?", (url,)).fetchone()
        if row and (row["status"] == "found" or time.time() - row["checked_at"] < RECHECK_NOT_FOUND):
            return (row["sfl_id"], row["path"]) if row["status"] == "found" else None
        found = self.search(name, lat, lon, country) if name else None
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sfl_map VALUES (?, ?, ?, ?, ?)",
                        (url, "found" if found else "not_found", found[0] if found else "", found[1] if found else "", int(time.time())))
        return found

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

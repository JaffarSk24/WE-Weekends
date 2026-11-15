"""Trains, through the public ticket shop of the Austrian railways (ÖBB):
its timetable covers most of Europe, and it prices the journeys it sells,
international ones included, for the exact party with children's ages.

When a journey has a timetable but no price, the fare is estimated from the
distance and marked as an estimate. The shop is asked politely: one request
at a time with a pause, under the service's own name, and every answer is
cached for half a day for all users."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from ..db import Database
from .road import km_between

log = logging.getLogger("weekends.rail")
SHOP = "https://shop.oebbtickets.at"
AGENT = "WE-Weekends/1.0 (+https://github.com/JaffarSk24/WE-Weekends)"
PAUSE = 0.6
TTL = 12 * 3600
STATION_KM = 30  # a station further than this from the place is no station of the place
EUR_PER_KM = 0.065  # an estimated second-class fare a km for an adult, as the shop's saver fares come out
RAIL_DETOUR = 1.25  # rails are longer than the straight line


@dataclass(frozen=True)
class Journey:
    depart: datetime
    arrive: datetime
    changes: int
    trains: str  # the kinds of train, like "RJX, REX"
    price: float  # for the whole party, EUR
    approx: bool  # the price is an estimate from the distance


def _passenger(i: int, kind: str, birth: str) -> dict:
    return {"me": False, "remembered": False, "challengedFlags": {"hasHandicappedPass": False, "hasAssistanceDog": False, "hasWheelchair": False,
                                                                  "hasAttendant": False},
            "relations": [], "cards": [], "birthdateChangeable": True, "birthdateDeletable": True, "nameChangeable": True,
            "passengerDeletable": True, "isSelected": True, "id": i, "type": kind, "birthDate": birth}


def passengers(adults: int, ages: tuple[int, ...], on: date) -> list[dict]:
    """The party as the shop counts it: the birth date of a child of each age."""
    out = [_passenger(i + 1, "ADULT", "1985-01-01") for i in range(adults)]
    for n, age in enumerate(ages):
        born = date(on.year - age - 1, on.month, min(on.day, 28)).isoformat()  # a little over the age, as on the day of travel
        out.append(_passenger(adults + n + 1, "CHILD" if age < 15 else "ADULT", born))
    return out


def estimate(a: tuple[float, float], b: tuple[float, float], adults: int, ages: tuple[int, ...]) -> float:
    """A fare from the distance. Children under 15 travel free with their
    parents, as on the Austrian railways."""
    persons = adults + sum(1 for x in ages if x >= 15)
    return round(km_between(a, b) * RAIL_DETOUR * EUR_PER_KM * persons, 2)


class Rail:
    def __init__(self, db: Database, client: httpx.Client | None = None) -> None:
        self.db = db
        self.client = client or httpx.Client(timeout=40.0, follow_redirects=False,
                                             headers={"User-Agent": AGENT, "Accept": "application/json", "Channel": "inet", "Lang": "en"})
        self.calls = 0
        self._last = 0.0
        self._session: tuple[dict, float] | None = None
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS rail_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    # -- plumbing ----------------------------------------------------------
    def _call(self, method: str, path: str, **kw):
        wait = PAUSE - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            self.calls += 1
            r = self.client.request(method, SHOP + path, headers={**self._headers(), **kw.pop("headers", {})}, **kw)
            r.raise_for_status()
            return r.json()
        finally:
            self._last = time.monotonic()

    def _headers(self) -> dict:
        """An anonymous session of the shop, as its page opens one; it lasts
        forty minutes and is opened again after."""
        if self._session is None or time.time() > self._session[1]:
            self._last = time.monotonic()
            r = self.client.get(f"{SHOP}/api/domain/v4/init", params={"userId": f"anonym-{uuid.uuid4().hex[:8]}", "cookies": "false"})
            r.raise_for_status()
            init = r.json()
            self._session = ({"AccessToken": init["accessToken"], "SessionId": init["sessionId"], "x-ts-supportid": init["supportId"]},
                             time.time() + int(init.get("sessionTimeout") or 2400) - 120)
        return self._session[0]

    def _cached(self, key: str, ttl: int, fetch):
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM rail_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < ttl:
            return json.loads(row["body"])
        try:
            value = fetch()
        except httpx.HTTPStatusError as exc:
            if not 400 <= exc.response.status_code < 500:
                log.warning("rail lookup failed: %s", exc.response.status_code)
                return None
            value = []  # refused for these dates, as beyond the published timetable: kept, not asked again all day
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("rail lookup failed: %s", type(exc).__name__)
            return None
        with self.db.tx() as con:
            con.execute("INSERT OR REPLACE INTO rail_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(value), int(time.time())))
        return value

    # -- stations ----------------------------------------------------------
    def station(self, name: str, near: tuple[float, float]) -> dict | None:
        """The shop's station for a place: the first match of its name that
        stands near the place."""
        def fetch():
            found = self._call("GET", "/api/hafas/v1/stations", params={"name": name[:60], "count": 6})
            for st in found or []:
                where = (st["latitude"] / 1e6, st["longitude"] / 1e6)
                if km_between(where, near) <= STATION_KM:
                    return st
            return {}

        st = self._cached(f"st:{name.lower()}:{near[0]:.2f},{near[1]:.2f}", 90 * 86400, fetch)
        return st or None

    # -- journeys ----------------------------------------------------------
    def journey(self, a: dict, b: dict, after: datetime, adults: int, ages: tuple[int, ...]) -> Journey | None:
        """The first good train journey from a to b leaving after the given
        time on that day, with its price for the party. Buses are taken only
        when there is no train."""
        party = f"{adults}:{','.join(map(str, ages))}"
        key = f"j:{a['number']}:{b['number']}:{after:%Y-%m-%dT%H}:{party}"

        def fetch():
            when = after.strftime("%Y-%m-%dT%H:%M:00.000")
            actions = self._call("POST", "/api/offer/v2/travelActions", json={
                "from": a, "to": b, "datetime": when, "customerVias": [], "ignoreHistory": True,
                "filter": {"productTypes": [], "history": True, "maxEntries": 5, "channel": "inet"}})
            action = next(x for x in actions["travelActions"] if x["entrypoint"]["id"] == "timetable")
            table = self._call("POST", "/api/hafas/v4/timetable", json={
                "travelActionId": action["id"], "datetimeDeparture": when, "count": 5, "from": a, "to": b, "timeout": {}, "sortType": "DEPARTURE",
                "filter": {"regionaltrains": False, "direct": False, "wheelchair": False, "bikes": False, "trains": False, "motorail": False,
                           "connections": []},
                "passengers": passengers(adults, ages, after.date()), "reverse": False,
                "debugFilter": {"noAggregationFilter": False, "noEqclassFilter": False, "noNrtpathFilter": False, "noPaymentFilter": False,
                                "useTripartFilter": False, "noVbxFilter": False, "noCategoriesFilter": False}})
            connections = table.get("connections") or []
            if not connections:
                return []
            ids = "&".join(f"connectionIds[]={c['id']}" for c in connections)
            offers = {o["connectionId"]: o.get("price") for o in (self._call("GET", f"/api/offer/v1/prices?{ids}").get("offers") or [])
                      if not o.get("offerError")}
            out = []
            for c in connections:
                kinds = [str((s.get("category") or {}).get("displayName") or "") for s in c.get("sections") or []]
                out.append({"depart": c["from"]["departure"], "arrive": c["to"]["arrival"], "changes": int(c.get("switches") or 0),
                            "trains": ", ".join(dict.fromkeys(k for k in kinds if k)), "bus": any(k.lower() == "bus" for k in kinds),
                            "price": offers.get(c["id"])})
            return out

        found = self._cached(key, TTL, fetch)
        if not found:
            return None
        same_day = [c for c in found if c["depart"][:10] == after.date().isoformat()]
        trains = [c for c in same_day if not c["bus"]] or same_day
        if not trains:
            return None
        best = min(trains, key=lambda c: (c["price"] is None, datetime.fromisoformat(c["arrive"])))
        here = (a["latitude"] / 1e6, a["longitude"] / 1e6)
        there = (b["latitude"] / 1e6, b["longitude"] / 1e6)
        price = best["price"] if best["price"] is not None else estimate(here, there, adults, ages)
        return Journey(depart=datetime.fromisoformat(best["depart"]), arrive=datetime.fromisoformat(best["arrive"]), changes=best["changes"],
                       trains=best["trains"][:40], price=float(price), approx=best["price"] is None)

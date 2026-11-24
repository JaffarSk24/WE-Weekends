"""Car trips: where the user lives, how far and how long the road is, and
what the fuel or the electricity for it costs.

- Addresses are found through Nominatim (OpenStreetMap), only when the user
  searches for one on the page.
- Roads come from the public OSRM server: one route for a hotel, one table
  for all the weekend cities around home.
- The route is cut into the countries it runs through (data/borders.json,
  Natural Earth), and each stretch is priced at that country's price: petrol
  and diesel from the EU Weekly Oil Bulletin, electricity from Eurostat's
  household prices. A country neither source covers takes the price of the
  last country before it on the way, where the tank was filled.

Both public servers ask for little traffic: one request a second at most,
under the service's own name, and every answer is cached."""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import time
import zipfile
from datetime import date, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree  # nosec B405: only the sheets of the EU bulletin, with declarations refused

import httpx

from ..db import Database

log = logging.getLogger("weekends.road")
DATA = Path(__file__).resolve().parents[1] / "data"
AGENT = "WE-Weekends/1.0 (+https://github.com/JaffarSk24/WE-Weekends)"
OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"
BULLETIN = ("https://energy.ec.europa.eu/document/download/264c2d0f-f161-4ea3-a777-78faae59bea0_en"
            "?filename=Weekly%20Oil%20Bulletin%20Weekly%20prices%20with%20Taxes.xlsx")
EUROSTAT = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_pc_204"
            "?format=JSON&lang=EN&freq=S&nrg_cons=KWH2500-4999&unit=KWH&tax=I_TAX&currency=EUR&lastTimePeriod=1")
PAUSE = 1.1  # seconds between requests to the public servers
ROUTE_TTL = 30 * 86400
PRICE_TTL = 7 * 86400
SAMPLE_KM = 5.0  # the route is looked at every this many km to tell the country
# The bulletin names countries in English.
BULLETIN_NAMES = {
    "Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Croatia": "HR", "Cyprus": "CY", "Czechia": "CZ", "Czech Republic": "CZ",
    "Denmark": "DK", "Estonia": "EE", "Finland": "FI", "France": "FR", "Germany": "DE", "Greece": "GR", "Hungary": "HU",
    "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Lithuania": "LT", "Luxembourg": "LU", "Malta": "MT", "Netherlands": "NL",
    "Poland": "PL", "Portugal": "PT", "Romania": "RO", "Slovakia": "SK", "Slovenia": "SI", "Spain": "ES", "Sweden": "SE",
}
EUROSTAT_CODES = {"EL": "GR", "UK": "GB"}


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


@lru_cache(maxsize=1)
def borders() -> list[tuple[str, tuple[float, float, float, float], list[list[float]]]]:
    """(country, bounding box, outline) for every outline."""
    path = DATA / "borders.json"
    out = []
    for code, rings in (json.loads(path.read_text()) if path.is_file() else {}).items():
        for ring in rings:
            lons, lats = [p[0] for p in ring], [p[1] for p in ring]
            out.append((code, (min(lons), min(lats), max(lons), max(lats)), ring))
    return out


def country_at(lat: float, lon: float) -> str | None:
    for code, (x0, y0, x1, y1), ring in borders():
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        if inside:
            return code
    return None


def by_country(points: list[tuple[float, float]]) -> list[tuple[str, float]]:
    """The km of a route in each country, in the order they come: the line is
    looked at every few km, and a point at sea or just outside an outline
    counts for the country before it."""
    stretches: list[list] = []
    last = None
    since = 0.0
    prev = points[0] if points else None
    for p in points[1:]:
        step = km_between(prev, p)
        since += step
        prev = p
        if since >= SAMPLE_KM or p is points[-1]:
            here = country_at(p[0], p[1]) or last
            if here is None:
                pass
            elif stretches and stretches[-1][0] == here:
                stretches[-1][1] += since
            else:
                stretches.append([here, since])
            if here:
                last = here
            since = 0.0
    return [(c, k) for c, k in stretches]


def _xml(raw: bytes) -> ElementTree.Element:
    """A sheet of the bulletin. A spreadsheet has no business declaring
    entities, so one that does is refused before parsing."""
    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise ValueError("unexpected XML declarations")
    return ElementTree.fromstring(raw)  # noqa: S314  # nosec B314


class Road:
    def __init__(self, db: Database, client: httpx.Client | None = None) -> None:
        self.db = db
        self.client = client or httpx.Client(timeout=40.0, follow_redirects=True, headers={"User-Agent": AGENT})
        self.calls = 0
        self._last = 0.0
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS road_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    # -- plumbing ----------------------------------------------------------
    def _cached(self, key: str, ttl: int, fetch):
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM road_cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < ttl:
            return json.loads(row["body"])
        try:
            value = fetch()
        except (httpx.HTTPError, ValueError, KeyError, IndexError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            source = next((name for name in ("geo", "hotel", "route", "table", "fuel") if key.startswith(f"{name}:")), "other")
            log.warning("road source failed for %s: %s", source, type(exc).__name__)
            return json.loads(row["body"]) if row else None  # an old answer beats none
        if value is not None:
            with self.db.tx() as con:
                con.execute("INSERT OR REPLACE INTO road_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(value), int(time.time())))
        return value

    def _get(self, url: str, **kw) -> httpx.Response:
        wait = PAUSE - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            self.calls += 1
            r = self.client.get(url, **kw)
            r.raise_for_status()
            return r
        finally:
            self._last = time.monotonic()

    # -- addresses ---------------------------------------------------------
    def find(self, query: str, lang: str) -> list[dict]:
        """Places matching what the user typed, to pick their home from."""
        query = " ".join(query.split())[:120]
        if len(query) < 3:
            return []

        def fetch():
            r = self._get(f"{NOMINATIM}/search", params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1,
                                                        "accept-language": lang})
            out = []
            for p in r.json():
                cc = str((p.get("address") or {}).get("country_code", "")).upper()
                if len(cc) == 2 and p.get("lat") and p.get("lon"):
                    out.append({"name": str(p.get("display_name", ""))[:160], "lat": round(float(p["lat"]), 5),
                                "lon": round(float(p["lon"]), 5), "country": cc})
            return out

        return self._cached(f"geo:{lang}:{query.lower()}", ROUTE_TTL, fetch) or []

    def find_hotel(self, name: str, country: str) -> list[dict]:
        """Places to stay with this name in the country, from OpenStreetMap."""
        query = " ".join(name.split())[:120]

        def fetch():
            r = self._get(f"{NOMINATIM}/search", params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1, "countrycodes": country.lower(),
                                                        "accept-language": "en"})
            out = []
            for p in r.json():
                if p.get("category") != "tourism" or p.get("type") not in ("hotel", "guest_house", "motel", "apartment", "hostel", "chalet", "resort"):
                    continue
                a = p.get("address") or {}
                out.append({"name": str(p.get("name") or "")[:120], "lat": round(float(p["lat"]), 5), "lon": round(float(p["lon"]), 5),
                            "place": str(a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or "")[:80]})
            return out

        return self._cached(f"hotel:{country}:{query.lower()}", ROUTE_TTL, fetch) or []

    # -- roads -------------------------------------------------------------
    def route(self, a: tuple[float, float], b: tuple[float, float]) -> dict | None:
        """{km, hours, countries: [[country, km], ...]} of the fastest road."""
        key = f"route:{a[0]:.3f},{a[1]:.3f}:{b[0]:.3f},{b[1]:.3f}"

        def fetch():
            r = self._get(f"{OSRM}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "full", "geometries": "geojson"})
            routes = r.json().get("routes") or []
            if not routes:
                return {"km": None}
            best = routes[0]
            points = [(lat, lon) for lon, lat in best["geometry"]["coordinates"]]
            return {"km": round(best["distance"] / 1000, 1), "hours": round(best["duration"] / 3600, 2),
                    "countries": [[c, round(k, 1)] for c, k in by_country(points)]}

        found = self._cached(key, ROUTE_TTL, fetch)
        return found if found and found.get("km") else None

    def around(self, home: tuple[float, float], places: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        """{place: (km, hours)} by road from home, for many places at once."""
        codes = sorted(places)
        key = f"table:{home[0]:.3f},{home[1]:.3f}:" + ",".join(codes)

        def fetch():
            coords = ";".join([f"{home[1]},{home[0]}"] + [f"{places[c][1]},{places[c][0]}" for c in codes])
            r = self._get(f"{OSRM}/table/v1/driving/{coords}", params={"sources": "0", "annotations": "duration,distance"})
            data = r.json()
            durations, distances = data["durations"][0], data["distances"][0]
            return {c: [round(distances[i + 1] / 1000, 1), round(durations[i + 1] / 3600, 2)]
                    for i, c in enumerate(codes) if durations[i + 1] is not None and distances[i + 1] is not None}

        found = self._cached(key, ROUTE_TTL, fetch) or {}
        return {c: (v[0], v[1]) for c, v in found.items()}

    # -- prices ------------------------------------------------------------
    def prices(self) -> dict:
        """{"petrol": {country: EUR a litre}, "diesel": {...}, "electric": {country: EUR a kWh}, "fuel_date": ...}"""
        fuel = self._cached("fuel:bulletin", PRICE_TTL, self._bulletin) or {}
        power = self._cached("fuel:eurostat", 30 * 86400, self._eurostat) or {}
        return {"petrol": fuel.get("petrol", {}), "diesel": fuel.get("diesel", {}), "fuel_date": fuel.get("date"), "electric": power}

    def _bulletin(self) -> dict:
        r = self._get(BULLETIN)
        book = zipfile.ZipFile(BytesIO(r.content))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")) for si in _xml(book.read("xl/sharedStrings.xml")).findall("m:si", ns)]
        rows = []
        for row in _xml(book.read("xl/worksheets/sheet1.xml")).iter(f"{{{ns['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", ns):
                v = c.find("m:v", ns)
                if v is None:
                    continue
                cells[re.sub(r"\d+", "", c.get("r", ""))] = shared[int(v.text)] if c.get("t") == "s" else v.text
            rows.append(cells)
        petrol, diesel, day = {}, {}, None
        for cells in rows:
            name = str(cells.get("A", "")).strip()
            if name.isdigit() and day is None:  # the date of the prices, as an Excel day number
                day = (date(1899, 12, 30) + timedelta(days=int(name))).isoformat()
            code = BULLETIN_NAMES.get(name)
            if not code:
                continue
            for column, table in (("B", petrol), ("C", diesel)):  # per 1000 litres
                try:
                    table[code] = round(float(cells[column]) / 1000, 3)
                except (KeyError, ValueError):
                    pass
        if len(petrol) < 20:
            raise ValueError("bulletin layout changed")
        return {"petrol": petrol, "diesel": diesel, "date": day}

    def _eurostat(self) -> dict:
        data = self._get(EUROSTAT).json()
        index = data["dimension"]["geo"]["category"]["index"]
        out = {}
        for code, i in index.items():
            value = data["value"].get(str(i))
            if value is not None and len(code) == 2:
                out[EUROSTAT_CODES.get(code, code)] = round(float(value), 4)
        if len(out) < 20:
            raise ValueError("eurostat layout changed")
        return out

    # -- the bill ----------------------------------------------------------
    def cost(self, car: dict, route: dict, home_country: str) -> float | None:
        """What one way of the route costs in fuel or electricity: the user's
        own price if given, else each country's price for its km."""
        if not route or not route.get("km"):
            return None
        per_km = float(car["per_100"]) / 100
        if car.get("price"):
            return round(route["km"] * per_km * float(car["price"]), 2)
        table = self.prices()[car["fuel"]]
        if not table:
            return None
        fallback = table.get(home_country) or statistics.median(table.values())
        total, last = 0.0, fallback
        for country, km in route.get("countries") or [[home_country, route["km"]]]:
            last = table.get(country) or last
            total += km * per_km * last
        counted = sum(km for _, km in route.get("countries") or []) or route["km"]
        total *= route["km"] / counted  # sea crossings and border gaps are priced like the rest
        return round(total, 2)

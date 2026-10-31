"""Flight search through Kiwi.com's public search endpoint (JSON-RPC over
HTTP, no key). Requests are spaced out and cached, and every search is
shared by all users of the same departure airport."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from ..db import Database

log = logging.getLogger("weekends.kiwi")

ENDPOINT = "https://mcp.kiwi.com"
MIN_INTERVAL = 1.0  # seconds between requests


@dataclass(frozen=True)
class Itinerary:
    origin: str
    dest: str
    city: str
    country: str
    price: float
    depart: datetime  # outbound departure, local time at origin
    arrive: datetime  # outbound arrival, local time at destination
    ret_depart: datetime
    back: datetime  # arrival home, local time
    airline: str
    flights: str
    url: str
    image: str

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.dest}-{self.depart:%Y%m%d%H%M}-{self.ret_depart:%Y%m%d%H%M}"

    @property
    def checkin(self) -> date:
        """Landing between midnight and 05:00 needs the bed from the evening before."""
        return self.arrive.date() - timedelta(days=1) if self.arrive.hour < 5 else self.arrive.date()

    @property
    def checkout(self) -> date:
        """A flight home just after midnight means leaving the room the day before."""
        return self.ret_depart.date() - timedelta(days=1) if self.ret_depart.hour < 5 else self.ret_depart.date()

    @property
    def nights(self) -> int:
        return max(0, (self.checkout - self.checkin).days)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value[:19])


def parse(item: dict[str, Any]) -> Itinerary | None:
    try:
        out, back = item["outbound"], item["inbound"]
        if out["stops"] != 0 or back["stops"] != 0:
            return None
        seg, rseg = out["segments"][0], back["segments"][0]
        return Itinerary(
            origin=seg["from"], dest=seg["to"], city=seg.get("toCity") or seg["to"], country="",
            price=float(item["price"]),
            depart=_dt(out["departureTime"]), arrive=_dt(out["arrivalTime"]),
            ret_depart=_dt(back["departureTime"]), back=_dt(back["arrivalTime"]),
            airline=seg.get("carrierName") or seg.get("carrier", ""),
            flights=f"{seg.get('carrier', '')}{seg.get('flightNumber', '')}/{rseg.get('carrier', '')}{rseg.get('flightNumber', '')}",
            url=str(item.get("bookingUrl", "")), image=str(item.get("imageId", "")),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class Kiwi:
    def __init__(self, db: Database, ttl_hours: float = 20) -> None:
        self.db = db
        self.ttl = int(ttl_hours * 3600)
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
        self._last = 0.0
        self.calls = 0
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS kiwi_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    def _post(self, args: dict) -> dict:
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search-flight", "arguments": args}}
        response = self.client.post(ENDPOINT, json=body, headers={"Accept": "application/json, text/event-stream"})
        response.raise_for_status()
        events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        data = json.loads(events[-1] if events else response.text)
        result = data.get("result") or {}
        if result.get("isError"):
            raise RuntimeError("kiwi error")
        if result.get("structuredContent"):
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])

    def search(self, country: str = "", **args: Any) -> list[Itinerary]:
        args = {k: v for k, v in args.items() if v is not None}
        args.setdefault("max_sector_stopovers", 0)
        args.setdefault("currency", "EUR")
        args.setdefault("sort", "price")
        key = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        now = int(time.time())
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM kiwi_cache WHERE key = ?", (key,)).fetchone()
        if row and now - row["fetched_at"] < self.ttl:
            data = json.loads(row["body"])
        else:
            for attempt in range(3):
                try:
                    data = self._post(args)
                    break
                except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
                    log.warning("kiwi attempt %d failed: %s", attempt + 1, type(exc).__name__)
                    time.sleep(5 * (attempt + 1))
            else:
                return []
            with self.db.tx() as con:
                con.execute("INSERT OR REPLACE INTO kiwi_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(data), now))
        found = []
        for item in data.get("itineraries", []):
            it = parse(item)
            if it:
                if country:
                    it = Itinerary(**{**it.__dict__, "country": country})
                found.append(it)
        return found

    def purge(self, older_than_days: int = 3) -> None:
        with self.db.tx() as con:
            con.execute("DELETE FROM kiwi_cache WHERE fetched_at < ?", (int(time.time()) - older_than_days * 86400,))


def kdate(d: date) -> str:
    return d.strftime("%d/%m/%Y")

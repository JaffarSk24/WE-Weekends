"""Weather from Open-Meteo (free, no key): the forecast for trips in the next
two weeks, the same dates last year for trips further away."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from ..db import Database

log = logging.getLogger("weekends.weather")


@dataclass(frozen=True)
class Weather:
    tmax: float
    rain_days: int
    days: int
    forecast: bool


class WeatherSource:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.client = httpx.Client(timeout=20.0, follow_redirects=False)
        with db.tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS weather_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER NOT NULL)")

    def get(self, lat: float, lon: float, start: date, end: date) -> Weather | None:
        today = date.today()
        forecast = end <= today + timedelta(days=15)
        if forecast:
            url = "https://api.open-meteo.com/v1/forecast"
            a, b = start, end
        else:
            url = "https://archive-api.open-meteo.com/v1/archive"
            a, b = _year_before(start), _year_before(end)
        key = f"{lat:.2f},{lon:.2f},{a},{b},{int(forecast)}"
        with self.db.tx() as con:
            row = con.execute("SELECT body, fetched_at FROM weather_cache WHERE key = ?", (key,)).fetchone()
        ttl = 6 * 3600 if forecast else 30 * 86400
        if row and time.time() - row["fetched_at"] < ttl:
            daily = json.loads(row["body"])
        else:
            try:
                response = self.client.get(url, params={
                    "latitude": round(lat, 3), "longitude": round(lon, 3), "start_date": a.isoformat(), "end_date": b.isoformat(),
                    "daily": "temperature_2m_max,precipitation_sum", "timezone": "auto",
                })
                response.raise_for_status()
                daily = response.json().get("daily") or {}
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("weather failed: %s", type(exc).__name__)
                return None
            with self.db.tx() as con:
                con.execute("INSERT OR REPLACE INTO weather_cache (key, body, fetched_at) VALUES (?, ?, ?)", (key, json.dumps(daily), int(time.time())))
        temps = [t for t in daily.get("temperature_2m_max") or [] if t is not None]
        rain = [r for r in daily.get("precipitation_sum") or [] if r is not None]
        if not temps:
            return None
        return Weather(tmax=sum(temps) / len(temps), rain_days=sum(1 for r in rain if r >= 2), days=len(temps), forecast=forecast)


def _year_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)

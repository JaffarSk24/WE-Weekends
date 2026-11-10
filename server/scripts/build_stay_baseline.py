"""Build weekends/data/stay_baseline.json: the usual nightly price in EUR of
a good hotel room for two in every destination city. The engine falls back on
it when no source has a price for the exact dates, so a trip always has a
total, marked as an estimate.

For each city a weekend five weeks ahead is priced: three well-rated hotels
through Xotelo, or the cheapest well-rated ones on Stayforlong where Xotelo
has nothing. Countries get the middle of their cities. Run every few months:

    python scripts/build_stay_baseline.py
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weekends.db import Database  # noqa: E402
from weekends.engine.hotels import Hotels, StayQuery, tripadvisor_keys  # noqa: E402
from weekends.engine.run import destinations  # noqa: E402
from weekends.engine.stayforlong import Stayforlong  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "weekends" / "data" / "stay_baseline.json"


def main() -> None:
    db = Database(str(Path(tempfile.gettempdir()) / "stay_baseline.sqlite"))
    sfl = Stayforlong(db)
    hotels = Hotels(db)
    today = date.today()
    friday = today + timedelta(days=(4 - today.weekday()) % 7 + 35)
    cities: dict[str, int] = {}

    def price(item: tuple[str, dict]) -> None:
        code, d = item
        q = StayQuery(city=d["city"], country=d["country"], iata=code, lat=d.get("lat"), lon=d.get("lon"), checkin=friday,
                      checkout=friday + timedelta(days=2), adults=2, children=(), min_rating=8.0, min_reviews=100, breakfast="any",
                      min_stars=0, lang="en")
        stay = None
        try:
            if code in tripadvisor_keys():
                stay = hotels._xotelo(q, tripadvisor_keys()[code])
            if stay is None and q.lat is not None:
                stay = sfl.around(q)
        except Exception as exc:  # one city must not stop the rest
            print(f"{code} failed: {type(exc).__name__}", flush=True)
        if stay and stay.price:
            cities[code] = round(stay.price / q.nights)
        print(f"{code} {d['city'][:25]:25} {cities.get(code, '-')} {stay.source if stay else ''}", flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:  # three cities at a time, six hotels each
        list(pool.map(price, sorted(destinations().items())))
    cities = dict(sorted(cities.items()))
    by_country: dict[str, list[int]] = {}
    for code, price in cities.items():
        by_country.setdefault(destinations()[code]["country"].upper(), []).append(price)
    out = {"cities": cities, "countries": {cc: round(statistics.median(v)) for cc, v in sorted(by_country.items())},
           "all": round(statistics.median(cities.values()))}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(cities)} of {len(destinations())} cities priced -> {OUT}")


if __name__ == "__main__":
    main()

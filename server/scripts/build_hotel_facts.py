"""Build weekends/data/hotel_facts.json from JSON-lines exports of Booking.com
resort search results (one hotel per line: id, name, url, country, city,
lat, lon, stars, score, reviews, ai, water_park, water_slide, kids_club,
kids_pool, beachfront).

Every hotel is matched once to TripAdvisor through Xotelo, the same way a
tracked hotel is, and kept only when the match stands within 3 km of the
Booking.com coordinates. The engine then prices it daily for the user's
dates. Run again whenever the exports are refreshed, then
add_stayforlong_ids.py for the hotels that are new:

    python scripts/build_hotel_facts.py exports/*.jsonl
"""
from __future__ import annotations

import json
import math
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weekends.db import Database  # noqa: E402
from weekends.engine.hotels import tripadvisor_keys  # noqa: E402
from weekends.engine.run import destinations  # noqa: E402
from weekends.engine.tracked import Tracked  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "weekends" / "data" / "hotel_facts.json"
MAX_KM = 3.0
NEAR_KM = 12.0
# Country-wide TripAdvisor areas. Their lists stop at about 3000 of the best
# hotels, which is enough to learn the town areas around each resort hotel.
COUNTRIES = {"tr": "g293969", "gr": "g189398", "cy": "g190372", "eg": "g294200", "es": "g187427", "bg": "g294451", "tn": "g293753",
             "it": "g187768", "hr": "g294453", "al": "g294445", "mt": "g190311", "pt": "g189100", "ae": "g295424", "ma": "g293730"}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def short_name(name: str) -> str:
    """The hotel's own name without what Booking.com appends to it, such as
    "- Ultra All Inclusive & Private Beach"."""
    return re.split(r"\s[-\u2013|]\s|,", name)[0].strip()


def load(paths: list[str]) -> dict[str, dict]:
    hotels: dict[str, dict] = {}
    for path in paths:
        for line in Path(path).read_text().splitlines():
            row = json.loads(line) if line.strip() else {}
            url = str(row.get("url") or "").split("?")[0]
            if row.get("error") or not url.startswith("https://www.booking.com/hotel/") or not row.get("stars"):
                continue
            h = hotels.setdefault(url, {
                "url": url, "name": row["name"], "cc": row["country"], "city": row.get("city") or "",
                "lat": row["lat"], "lon": row["lon"], "stars": int(row["stars"]), "score": row.get("score") or 0,
                "reviews": row.get("reviews") or 0, "ai": False, "aqua": False, "slide": False, "kids": False, "beach": False,
            })
            h["ai"] = h["ai"] or row.get("ai") is True
            h["aqua"] = h["aqua"] or bool(row.get("water_park"))
            h["slide"] = h["slide"] or bool(row.get("water_slide"))
            h["kids"] = h["kids"] or bool(row.get("kids_club"))
            h["beach"] = h["beach"] or bool(row.get("beachfront"))
    return hotels


def main(paths: list[str]) -> None:
    hotels = load(paths)
    old = {h["url"]: h for h in json.loads(OUT.read_text())} if OUT.exists() else {}
    keys = tripadvisor_keys()
    geos: dict[str, list[str]] = {}
    for code, d in destinations().items():
        if code in keys:
            geos.setdefault(d.get("country", "").lower(), []).append(keys[code])
    cache = Path(tempfile.gettempdir()) / "hotel_facts_cache.sqlite"
    tracked = Tracked(Database(str(cache)))
    seen: dict[str, list[tuple[float, float, str]]] = {}
    for cc in {h["cc"] for h in hotels.values()} & set(COUNTRIES):
        points = seen.setdefault(cc, [])
        for item in tracked._hotels(COUNTRIES[cc], [40]):
            g = item.get("geo") or {}
            if g.get("latitude") is not None and "-" in str(item.get("key", "")):
                points.append((g["latitude"], g["longitude"], str(item["key"]).split("-")[0]))
    kept = []
    for h in sorted(hotels.values(), key=lambda x: (x["cc"], x["city"], x["name"])):
        if "sfl" in old.get(h["url"], {}):  # add_stayforlong_ids.py fills it for the new ones
            h["sfl"] = old[h["url"]]["sfl"]
        if old.get(h["url"], {}).get("ta"):
            h["ta"] = old[h["url"]]["ta"]
        else:
            near: dict[str, int] = {}
            for lat, lon, geo in seen.get(h["cc"], []):
                if km((h["lat"], h["lon"]), (lat, lon)) <= NEAR_KM:
                    near[geo] = near.get(geo, 0) + 1
            local = sorted(near, key=lambda g: -near[g])[:4]
            m = tracked.resolve(h["url"], short_name(h["name"]), local + geos.get(h["cc"], []) + ([COUNTRIES[h["cc"]]] if h["cc"] in COUNTRIES else []))
            close = m.status == "found" and m.lat is not None and km((h["lat"], h["lon"]), (m.lat, m.lon)) <= MAX_KM
            h["ta"] = m.key if close else ""
        kept.append(h)
        print(f"{h['cc']} {h['name'][:50]:50} {h['ta'] or '-'}", flush=True)
    OUT.write_text(json.dumps(kept, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(kept)} hotels, {sum(1 for h in kept if h['ta'])} priced through TripAdvisor -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])

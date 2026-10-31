"""Builds weekends/data/destinations.json and weekends/data/tripadvisor.json
for a list of destination airports, from Wikidata (CC0): the city each
airport serves, its name in English, Russian and Slovak, the airport's
coordinates (for the weather) and the city's TripAdvisor id (for hotel
rates). Tags (sea, mountains, sights) come from weekends/data/tags.json,
which is curated by hand.

    python3 -I scripts/build_destinations.py routes.json
where routes.json maps IATA code -> {"city": ..., "country": ...}.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "weekends" / "data"
QUERY = """SELECT ?iata ?ta ?en ?ru ?sk ?coord WHERE {
  VALUES ?iata { %s }
  ?a wdt:P238 ?iata. ?a wdt:P931 ?city.
  OPTIONAL { ?city wdt:P3134 ?ta }
  OPTIONAL { ?a wdt:P625 ?coord }
  OPTIONAL { ?city rdfs:label ?en FILTER(lang(?en) = "en") }
  OPTIONAL { ?city rdfs:label ?ru FILTER(lang(?ru) = "ru") }
  OPTIONAL { ?city rdfs:label ?sk FILTER(lang(?sk) = "sk") }
}"""


def sparql(codes):
    query = QUERY % " ".join(f'"{c}"' for c in codes)
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": query})
    request = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json",
                                                   "User-Agent": "WE-Weekends-data-build/1.0 (welcome@whiteeagles.sk)"})
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 (fixed https URL)
        return json.load(response)["results"]["bindings"]


routes = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tags = json.loads((DATA / "tags.json").read_text(encoding="utf-8"))
codes = sorted(c for c in routes if len(c) == 3 and c.isalpha() and c.isupper())
found: dict[str, dict] = {}
for i in range(0, len(codes), 60):
    for row in sparql(codes[i:i + 60]):
        v = {k: x["value"] for k, x in row.items()}
        d = found.setdefault(v["iata"], {"names": {}})
        for lang in ("en", "ru", "sk"):
            if v.get(lang) and lang not in d["names"]:
                d["names"][lang] = v[lang]
        if v.get("coord") and "lat" not in d:
            lon, lat = v["coord"].removeprefix("Point(").removesuffix(")").split()
            d["lat"], d["lon"] = round(float(lat), 4), round(float(lon), 4)
        if v.get("ta") and v["ta"].isdigit():
            d["ta"] = "g" + v["ta"]
    time.sleep(2)

destinations, tripadvisor = {}, {}
for code in codes:
    d = found.get(code, {"names": {}})
    destinations[code] = {
        "city": d["names"].get("en") or routes[code].get("city", code),
        "names": d["names"],
        "country": routes[code].get("country", ""),
        "lat": d.get("lat"), "lon": d.get("lon"),
        "tags": tags.get(code, []),
    }
    if d.get("ta"):
        tripadvisor[code] = d["ta"]
(DATA / "destinations.json").write_text(json.dumps(destinations, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
(DATA / "tripadvisor.json").write_text(json.dumps(tripadvisor, indent=1, sort_keys=True), encoding="utf-8")
print(len(destinations), "destinations,", sum(1 for d in destinations.values() if d["lat"]), "with coordinates,",
      len(tripadvisor), "with TripAdvisor ids,", sum(1 for d in destinations.values() if d["tags"]), "tagged")

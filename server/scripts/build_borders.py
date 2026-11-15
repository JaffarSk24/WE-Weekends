"""Build weekends/data/borders.json: country outlines of Europe and around it,
for telling which country each stretch of a car route runs through, so the
fuel is priced by the country it is bought in. From Natural Earth, 1:50m
admin 0 countries (public domain), cut to the region and rounded to about
100 m:

    python scripts/build_borders.py ne_50m_admin_0_countries.geojson
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "weekends" / "data" / "borders.json"
WEST, EAST, SOUTH, NORTH = -32.0, 50.0, 26.0, 72.0


def main(path: str) -> None:
    out: dict[str, list] = {}
    for feature in json.loads(Path(path).read_text())["features"]:
        props = feature["properties"]
        code = props.get("ISO_A2_EH") if props.get("ISO_A2_EH") not in (None, "-99") else props.get("ISO_A2")
        geom = feature["geometry"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for polygon in polygons:
            ring = polygon[0]  # holes are ignored: lakes and enclaves change no fuel bill
            lons, lats = [p[0] for p in ring], [p[1] for p in ring]
            if max(lons) < WEST or min(lons) > EAST or max(lats) < SOUTH or min(lats) > NORTH or not code or code == "-99":
                continue
            out.setdefault(code, []).append([[round(x, 3), round(y, 3)] for x, y in ring])
    OUT.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"{len(out)} countries, {sum(len(r) for v in out.values() for r in v)} points -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])

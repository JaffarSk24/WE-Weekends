"""Add the Stayforlong id and page path to every hotel of
weekends/data/hotel_facts.json, so the engine can price hotel suggestions on
Stayforlong too, also the ones TripAdvisor has no prices for. Hotels that
already have one keep it. Run after build_hotel_facts.py:

    python scripts/add_stayforlong_ids.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_hotel_facts import OUT, short_name  # noqa: E402
from weekends.db import Database  # noqa: E402
from weekends.engine.stayforlong import Stayforlong  # noqa: E402


def main() -> None:
    hotels = json.loads(OUT.read_text())
    sfl = Stayforlong(Database(str(Path(tempfile.gettempdir()) / "hotel_facts_sfl.sqlite")))
    for h in hotels:
        if "sfl" not in h:
            found = sfl.search(short_name(h["name"]), h["lat"], h["lon"], h["cc"])
            h["sfl"] = f"{found[0]}:{found[1]}" if found else ""
        print(f"{h['cc']} {h['name'][:50]:50} {h['sfl'] or '-'}", flush=True)
    OUT.write_text(json.dumps(hotels, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(hotels)} hotels, {sum(1 for h in hotels if h['sfl'])} on Stayforlong -> {OUT}")


if __name__ == "__main__":
    main()

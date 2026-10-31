"""Builds weekends/data/visa.json from the passport-index dataset
(github.com/ilyankou/passport-index-dataset, MIT, passport-index-tidy-iso2.csv).

Only restricted pairs are kept: {passport: {destination: code}} with codes
v = visa required, e = e-visa, a = visa on arrival, t = electronic travel
authorisation. Anything missing is visa-free.

    python3 -I scripts/build_visa.py path/to/passport-index-tidy-iso2.csv
"""
import csv
import json
import sys
from pathlib import Path

CODES = {"visa required": "v", "e-visa": "e", "visa on arrival": "a", "eta": "t"}

out: dict[str, dict[str, str]] = {}
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        code = CODES.get(row["Requirement"].strip().lower())
        passport, dest = row["Passport"].strip(), row["Destination"].strip()
        if code and len(passport) == 2 and len(dest) == 2 and passport.isalpha() and dest.isalpha():
            out.setdefault(passport, {})[dest] = code
target = Path(__file__).resolve().parents[1] / "weekends" / "data" / "visa.json"
target.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True), encoding="utf-8")
print(len(out), "passports,", sum(len(v) for v in out.values()), "restricted pairs")

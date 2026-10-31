"""Entry rules by passport, with the waivers that EU residence permits give.

Base data: the passport-index dataset (see scripts/build_visa.py). Consular
visas cannot be arranged for a weekend, so those countries are dropped; an
e-visa, a visa on arrival or a travel authorisation is shown as a note."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

SCHENGEN = frozenset(
    "AT BE BG HR CZ DK EE FI FR DE GR HU IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH".split()
)
EU_EEA = SCHENGEN | {"CY", "IE"}

# Countries that let holders of a Schengen residence permit in without a
# visa, whatever their passport. Kept short and conservative on purpose:
# Ireland and the United Kingdom are not here, Kosovo is not here.
SCHENGEN_RESIDENCE_WAIVER = frozenset("AL BA ME MK RS CY GE AM MX CO PE CR PA DO".split())


@lru_cache(maxsize=1)
def _matrix() -> dict[str, dict[str, str]]:
    return json.loads((DATA / "visa.json").read_text(encoding="utf-8"))


def status(citizenship: str | None, residence: str | None, dest: str) -> str:
    """free, residence (free thanks to the permit), eta, evisa, on_arrival,
    visa (dropped) or unknown (no citizenship given)."""
    if citizenship is None:
        return "unknown"
    if dest == citizenship:
        return "free"
    if citizenship in EU_EEA and dest in EU_EEA:
        return "free"
    code = _matrix().get(citizenship, {}).get(dest)
    if code is None:
        return "free"
    if residence:
        if residence in SCHENGEN and (dest in SCHENGEN or dest in SCHENGEN_RESIDENCE_WAIVER):
            return "residence"
        if dest == residence:
            return "residence"
    return {"v": "visa", "e": "evisa", "a": "on_arrival", "t": "eta"}[code]


def allowed(entry: str) -> bool:
    return entry != "visa"

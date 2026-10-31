"""Places a user never wants to see: destination cities and resort towns.

Names are compared as whole words, so excluding Side never hits a hotel
called Seaside."""
from __future__ import annotations

import re
import unicodedata

from ..models import GEO


def words(text: str) -> tuple[str, ...]:
    ascii_text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()
    return tuple(w for w in re.split(r"[^a-z0-9]+", ascii_text) if w)


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


class Exclusions:
    def __init__(self, place_ids: list[str]) -> None:
        places = GEO.get("places", {})
        self.iatas = {places[p]["iata"] for p in place_ids if p in places and places[p].get("iata")}
        self.patterns = [tuple(m.split("_")) for p in place_ids if p in places for m in places[p]["match"]]

    def hits(self, text: str) -> bool:
        tokens = words(text)
        return any(_contains(tokens, pattern) for pattern in self.patterns)

    def destination(self, iata: str, city: str) -> bool:
        return iata in self.iatas or self.hits(city)


def place_of(text: str) -> str | None:
    """The known place a hotel page's location text names, if any."""
    tokens = words(text)
    for pid, place in GEO.get("places", {}).items():
        if any(_contains(tokens, tuple(m.split("_"))) for m in place["match"]):
            return pid
    return None

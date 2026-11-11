"""Hard filters and the score of one trip for one user.

The score is a penalty in euro-like points: lower is better. Each factor is
multiplied by the user's weight (0..10, 5 is neutral), so the presets and
sliders on the settings page change the order directly."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .calendar import SLOT_PENALTY, UserCalendar, outbound_slot, return_slot
from .kiwi import Itinerary
from .weather import Weather


@dataclass
class Candidate:
    it: Itinerary
    mode: str  # solo, family, diamond
    flight_price: float
    entry: str
    tags: tuple[str, ...]
    workdays_lost: int
    school_days_lost: int
    out_slot: str
    ret_slot: str
    hotel_price: float | None = None
    hotel_name: str = ""
    hotel_rating: float | None = None
    hotel_reviews: int | None = None
    hotel_place: str | None = None
    hotel_url: str = ""
    hotel_source: str = "link"
    hotel_approx: bool = False
    hotel_seller: str = ""
    transfer: float = 0.0
    weather: Weather | None = None
    usual: float | None = None
    penalty: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.flight_price + (self.hotel_price or 0) + self.transfer


def evaluate(it: Itinerary, cal: UserCalendar, family: bool, nights: tuple[int, int], today: date, horizon: int) -> tuple[int, int, str, str] | None:
    """Hard filters shared by every mode. None means the trip is dropped."""
    if it.depart.date() <= today or (it.depart.date() - today).days > horizon:
        return None
    if not nights[0] <= it.nights <= nights[1]:
        return None
    if cal.busy_conflict(it.depart.date(), it.back.date(), family):
        return None
    lost = cal.workdays_lost(it.depart, it.back)
    if lost > cal.max_lost:
        return None
    school = cal.school_days_lost(it.depart, it.back) if family else 0
    if school > cal.max_lost:
        return None
    return lost, school, outbound_slot(it.depart, cal), return_slot(it.ret_depart, it.back, cal)


def score(c: Candidate, settings: dict, budget: float, ratings: dict[str, tuple[int, int]]) -> float:
    w = settings["rating"]["weights"]
    pr = settings["priorities"]
    k = lambda name: w[name] / 5.0  # noqa: E731
    p = 0.0
    # price against budget: a trip at the full budget costs 60 points at weight 5
    p += 60 * (c.total / max(budget, 1)) * k("price")
    # flight times
    p += (SLOT_PENALTY[c.out_slot] + SLOT_PENALTY[c.ret_slot]) * k("times")
    # lost workdays and school days
    p += (30 * c.workdays_lost + 40 * c.school_days_lost) * k("workdays")
    # what the place is good for
    level = {"sea": pr["sea"], "mountains": pr["mountains"], "sights": pr["sights"]}
    best = max((level[t] for t in c.tags if t in level), default=-1)
    if best == -1:
        p += 15
    else:
        p += {3: -25, 2: -12, 1: 0, 0: 20}[best]
    # weather: warm and dry is better, mostly for the sea
    if c.weather:
        want = 24 if "sea" in c.tags and pr["sea"] >= 2 else 16
        p += (max(0.0, want - c.weather.tmax) * 2 + c.weather.rain_days * 6) * k("weather")
    # the stay
    if c.hotel_rating:
        p += max(0.0, 9.0 - c.hotel_rating) * 8 * k("stay")
    # cheaper than usual for this route and month
    # one adult's fare on both sides: flight_price may be the whole family's
    if c.usual and c.it.price < c.usual:
        p -= min(40.0, 60 * (1 - c.it.price / c.usual)) * k("discount")
    # soft ratings from the buttons, fading out over 60 days
    now = time.time()
    for aspect in (f"city:{c.it.dest}", f"country:{c.it.country}", f"out:{c.out_slot}", f"back:{c.ret_slot}"):
        if aspect in ratings:
            value, at = ratings[aspect]
            fade = max(0.0, 1 - (now - at) / (60 * 86400))
            p += (-12 if value > 0 else 25) * fade
    return round(p, 1)

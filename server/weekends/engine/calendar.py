"""Workdays, public holidays, school holidays and busy dates of one user."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import holidays as holidays_lib

DATA = Path(__file__).resolve().parents[1] / "data"


@lru_cache(maxsize=32)
def public_holidays(country: str, year: int) -> frozenset[date]:
    return frozenset(holidays_lib.country_holidays(country, years=[year]).keys())


@lru_cache(maxsize=1)
def _school() -> dict:
    return json.loads((DATA / "school.json").read_text(encoding="utf-8"))


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class UserCalendar:
    def __init__(self, settings: dict) -> None:
        cal = settings["calendar"]
        self.country = cal["holiday_country"]
        self.workdays = set(cal["workdays"])  # ISO weekday numbers, 1 = Monday
        self.max_lost = cal["max_lost_workdays"]
        self.busy = [(date.fromisoformat(b["start"]), date.fromisoformat(b["end"]), b["scope"]) for b in cal["busy"]]
        self.school_ranges: list[tuple[date, date]] = []
        school = cal["school"]
        if school["mode"] == "manual":
            self.school_ranges = [(date.fromisoformat(r["start"]), date.fromisoformat(r["end"])) for r in school["manual"]]
        elif school["mode"] == "region" and school["region"]:
            data = _school().get(school["region"][:2], {})
            ranges = list(data.get("common", []))
            if school["region"] in data.get("spring", {}):
                ranges.append(data["spring"][school["region"]])
            self.school_ranges = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in ranges]
        self.has_school = school["mode"] != "none"

    def is_holiday(self, d: date) -> bool:
        return d in public_holidays(self.country, d.year)

    def is_workday(self, d: date) -> bool:
        return d.isoweekday() in self.workdays and not self.is_holiday(d)

    def is_school_day(self, d: date) -> bool:
        if not self.has_school or d.isoweekday() > 5 or self.is_holiday(d):
            return False
        return not any(a <= d <= b for a, b in self.school_ranges)

    def lost_days(self, depart: datetime, back: datetime, day_check) -> int:
        """Days of `day_check` kind spent on the trip. The departure day is lost
        when the flight leaves before 17:00, the day of arrival home when the
        plane lands after 09:00, every day in between is lost."""
        lost = 0
        if day_check(depart.date()) and depart.hour < 17:
            lost += 1
        if back.date() != depart.date() and day_check(back.date()) and (back.hour, back.minute) > (9, 0):
            lost += 1
        for d in _days(depart.date() + timedelta(days=1), back.date() - timedelta(days=1)):
            lost += day_check(d)
        return lost

    def workdays_lost(self, depart: datetime, back: datetime) -> int:
        return self.lost_days(depart, back, self.is_workday)

    def school_days_lost(self, depart: datetime, back: datetime) -> int:
        return self.lost_days(depart, back, self.is_school_day) if self.has_school else 0

    def busy_conflict(self, start: date, end: date, family: bool) -> bool:
        for a, b, scope in self.busy:
            if scope == "family" and not family:
                continue
            if a <= end and start <= b:
                return True
        return False

    def holiday_inside(self, start: date, end: date) -> bool:
        return any(self.is_holiday(d) for d in _days(start, end))

    def upcoming_school_ranges(self, today: date, horizon: int) -> list[tuple[date, date]]:
        last = today + timedelta(days=horizon)
        return [(a, b) for a, b in self.school_ranges if b >= today + timedelta(days=2) and a <= last]


def outbound_slot(depart: datetime, cal: UserCalendar) -> str:
    """Early morning or the evening before a day off is best; a departure in
    the afternoon of a day off eats that day."""
    t = (depart.hour, depart.minute)
    if t <= (10, 0):
        return "morning"
    if cal.is_workday(depart.date()) and t >= (17, 0):
        return "evening_before"
    if t <= (13, 0):
        return "midday"
    return "day_lost"


def return_slot(ret_depart: datetime, back: datetime, cal: UserCalendar) -> str:
    if cal.is_workday(back.date()) and (back.hour, back.minute) <= (9, 0):
        return "before_work"
    if (ret_depart.hour, ret_depart.minute) >= (17, 0) or ret_depart.hour < 3:
        return "evening"
    if ret_depart.hour >= 13:
        return "afternoon"
    return "day_lost"


SLOT_PENALTY = {"morning": 0, "evening_before": 0, "midday": 10, "day_lost": 40,
                "before_work": 0, "evening": 0, "afternoon": 10}

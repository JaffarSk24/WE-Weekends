"""The settings schema. Every field is bounded: unknown keys, oversized
lists, out-of-range numbers and free-form URLs are rejected before anything
reaches the database or the search engine."""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def _load_geo() -> dict:
    candidates = [
        os.environ.get("GEO_PATH", ""),
        str(Path(__file__).resolve().parent / "geo.json"),
        str(Path(__file__).resolve().parents[2] / "web" / "assets" / "geo.json"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    raise RuntimeError("geo.json not found")


GEO = _load_geo()
AIRPORTS = frozenset(GEO["airports"])
GROUPS = frozenset(GEO["groups"])
HOLIDAY_COUNTRIES = frozenset(GEO["holiday_countries"])
EU_RESIDENCE = frozenset(GEO["eu_residence"])
COUNTRIES = frozenset(GEO["countries"])
PLACES = frozenset(GEO.get("places", {}))
SCHOOL_REGIONS = frozenset(r for regions in GEO["school_regions"].values() for r in regions)

Lang = Literal["ru", "sk", "en"]
Freq = Literal["daily", "3w", "2w", "weekly"]


def _country(value: str) -> str:
    if value not in COUNTRIES:
        raise ValueError("unknown country")
    return value


ISO2 = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$"), AfterValidator(_country)]


def _airport(value: str) -> str:
    if value not in AIRPORTS:
        raise ValueError("unknown airport")
    return value


def _group(value: str) -> str:
    if value not in GROUPS:
        raise ValueError("unknown group")
    return value


Airport = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$"), AfterValidator(_airport)]
Group = Annotated[str, StringConstraints(pattern=r"^[A-Z_]{2,20}$"), AfterValidator(_group)]


def _place(value: str) -> str:
    if value not in PLACES:
        raise ValueError("unknown place")
    return value


Place = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{2,40}$"), AfterValidator(_place)]


def _unique(values: list) -> list:
    if len(set(values)) != len(values):
        raise ValueError("duplicates")
    return values


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=False)


class DateRange(Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _check(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("end before start")
        if (self.end - self.start).days > 120:
            raise ValueError("range longer than 120 days")
        today = date.today()
        if self.end < today - timedelta(days=400) or self.start > today + timedelta(days=800):
            raise ValueError("range too far from today")
        return self


class Busy(DateRange):
    scope: Literal["all", "family"] = "all"


class Profile(Strict):
    origins: Annotated[list[Airport], Field(min_length=1, max_length=6), AfterValidator(_unique)] = ["BTS"]
    citizenship: ISO2 | None = None
    eu_residence: ISO2 | None = None

    @field_validator("eu_residence")
    @classmethod
    def _residence(cls, value: str | None) -> str | None:
        if value is not None and value not in EU_RESIDENCE:
            raise ValueError("not an EU/EEA country")
        return value


class Priorities(Strict):
    sea: Annotated[int, Field(ge=0, le=3)] = 3
    mountains: Annotated[int, Field(ge=0, le=3)] = 1
    sights: Annotated[int, Field(ge=0, le=3)] = 1


class Geography(Strict):
    groups: Annotated[list[Group], Field(max_length=20), AfterValidator(_unique)] = ["EUROPE", "NORTH_AFRICA", "MIDDLE_EAST", "CAUCASUS"]
    include: Annotated[list[ISO2], Field(max_length=250), AfterValidator(_unique)] = []
    exclude: Annotated[list[ISO2], Field(max_length=250), AfterValidator(_unique)] = []
    exclude_places: Annotated[list[Place], Field(max_length=50), AfterValidator(_unique)] = []


class Solo(Strict):
    enabled: bool = True
    budget_eur: Annotated[int, Field(ge=30, le=10_000)] = 300


class Family(Strict):
    enabled: bool = False
    adults: Annotated[int, Field(ge=1, le=4)] = 2
    children: Annotated[list[Annotated[int, Field(ge=0, le=17)]], Field(max_length=6)] = []
    budget_eur: Annotated[int, Field(ge=50, le=30_000)] = 500


class Budgets(Strict):
    solo: Solo = Solo()
    family: Family = Family()
    include_transfer: bool = True


class SchoolHolidays(Strict):
    mode: Literal["none", "region", "manual"] = "none"
    region: str | None = None
    manual: Annotated[list[DateRange], Field(max_length=12)] = []

    @model_validator(mode="after")
    def _check(self) -> SchoolHolidays:
        if self.region is not None and self.region not in SCHOOL_REGIONS:
            raise ValueError("unknown school region")
        if self.mode == "region" and self.region is None:
            raise ValueError("region required")
        return self


class Calendar(Strict):
    holiday_country: str = "SK"
    workdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(max_length=7), AfterValidator(_unique)] = [1, 2, 3, 4, 5]
    max_lost_workdays: Annotated[int, Field(ge=0, le=2)] = 1
    busy: Annotated[list[Busy], Field(max_length=30)] = []
    school: SchoolHolidays = SchoolHolidays()

    @field_validator("holiday_country")
    @classmethod
    def _country(cls, value: str) -> str:
        if value not in HOLIDAY_COUNTRIES:
            raise ValueError("unsupported holiday country")
        return value


class Trip(Strict):
    nights_min: Annotated[int, Field(ge=1, le=7)] = 2
    nights_max: Annotated[int, Field(ge=1, le=14)] = 3
    horizon_days: Annotated[int, Field(ge=14, le=180)] = 90

    @model_validator(mode="after")
    def _check(self) -> Trip:
        if self.nights_max < self.nights_min:
            raise ValueError("nights_max below nights_min")
        return self


class Stay(Strict):
    min_rating: Annotated[float, Field(ge=6, le=10, multiple_of=0.1)] = 8.0
    min_reviews: Annotated[int, Field(ge=0, le=5000)] = 50
    breakfast: Literal["any", "prefer", "require"] = "prefer"
    min_stars: Annotated[int, Field(ge=0, le=5)] = 0


Weight = Annotated[int, Field(ge=0, le=10)]


class Weights(Strict):
    price: Weight = 10
    times: Weight = 5
    workdays: Weight = 6
    weather: Weight = 3
    stay: Weight = 3
    discount: Weight = 6


PRESETS: dict[str, Weights] = {
    "cheap": Weights(price=10, times=5, workdays=6, weather=3, stay=3, discount=6),
    "comfort": Weights(price=5, times=8, workdays=7, weather=6, stay=8, discount=4),
    "kids": Weights(price=6, times=7, workdays=5, weather=7, stay=9, discount=5),
}


class Rating(Strict):
    preset: Literal["cheap", "comfort", "kids", "custom"] = "cheap"
    weights: Weights = Weights()


class Block(Strict):
    enabled: bool = True
    freq: Freq = "daily"


class Weekends(Block):
    pass


class Diamonds(Block):
    max_price_eur: Annotated[int, Field(ge=10, le=300)] = 50


class Wow(Block):
    freq: Freq = "2w"
    max_per_person_night: Annotated[int, Field(ge=10, le=500)] = 70


_BOOKING_PATH = r"^/hotel/[a-z]{2}/[a-z0-9-]{1,120}(\.[a-z]{2}(-[a-z]{2})?)?\.html$"


def _booking_url(value: str) -> str:
    """Accepts only a Booking hotel page and rebuilds it without query or
    fragment. The server never fetches these URLs."""
    if len(value) > 2048:
        raise ValueError("URL too long")
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or parts.hostname not in ("www.booking.com", "booking.com"):
        raise ValueError("not a booking.com link")
    if parts.port is not None or parts.username or parts.password:
        raise ValueError("not a booking.com link")
    path = parts.path.lower()
    if not re.fullmatch(_BOOKING_PATH, path):
        raise ValueError("not a Booking hotel page")
    path = re.sub(r"\.[a-z]{2}(-[a-z]{2})?\.html$", ".html", path)
    return "https://www.booking.com" + path


BookingUrl = Annotated[str, AfterValidator(_booking_url)]


class Suggest(Strict):
    enabled: bool = False
    max_hotel_eur: Annotated[int, Field(ge=100, le=50_000)] = 3500
    min_stars: Annotated[int, Field(ge=0, le=5)] = 5
    all_inclusive: bool = True
    aquapark: bool = False
    min_rating: Annotated[float, Field(ge=6, le=10, multiple_of=0.1)] = 8.5


class Vacation(Block):
    enabled: bool = False
    freq: Freq = "weekly"
    party: Literal["solo", "family"] = "family"
    date_options: Annotated[list[DateRange], Field(max_length=4)] = []
    countries: Annotated[list[ISO2], Field(max_length=30), AfterValidator(_unique)] = []
    hotels: Annotated[list[BookingUrl], Field(max_length=10), AfterValidator(_unique)] = []
    suggest: Suggest = Suggest()


class Blocks(Strict):
    weekends: Weekends = Weekends()
    diamonds: Diamonds = Diamonds(freq="2w")
    wow: Wow = Wow()
    vacation: Vacation = Vacation()


class Digest(Strict):
    hour: Annotated[int, Field(ge=5, le=22)] = 10
    weekly_day: Annotated[int, Field(ge=1, le=7)] = 1


class UserSettings(Strict):
    schema_version: Literal[1] = 1
    profile: Profile = Profile()
    geography: Geography = Geography()
    priorities: Priorities = Priorities()
    budgets: Budgets = Budgets()
    calendar: Calendar = Calendar()
    trip: Trip = Trip()
    stay: Stay = Stay()
    rating: Rating = Rating()
    blocks: Blocks = Blocks()
    digest: Digest = Digest()

    @model_validator(mode="after")
    def _check(self) -> UserSettings:
        if self.rating.preset != "custom":
            self.rating.weights = PRESETS[self.rating.preset].model_copy()
        return self


class SaveRequest(Strict):
    version: Annotated[int, Field(ge=0)]
    lang: Lang
    settings: UserSettings


class VerifyRequest(Strict):
    nonce: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{32}$")]
    secret: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
    code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class TelegramAuthRequest(Strict):
    init_data: Annotated[str, StringConstraints(min_length=20, max_length=4096)]


class PauseRequest(Strict):
    paused: bool


class DeleteRequest(Strict):
    confirm: Literal["DELETE"]

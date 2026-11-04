"""The engine loop.

Every few minutes: make sure today's shared searches exist for every
(departure airport, country) pair that some active user needs, then send the
digest to each user whose hour has come. Searches are cached, so a restart
costs nothing and a new user's first digest arrives within minutes.

    python -m weekends.engine.run            # the loop (container command)
    python -m weekends.engine.run --dry-run CHAT_ID   # print one digest
"""
from __future__ import annotations

import json
import logging
import secrets
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config as config_module
from ..db import Database
from ..models import GEO, UserSettings
from ..telegram import Telegram
from . import digest as dg
from .calendar import UserCalendar
from .entry import allowed, status
from .hotels import Hotels, StayQuery, tripadvisor_keys
from .kiwi import Itinerary, Kiwi, kdate
from .places import Exclusions
from .score import Candidate, evaluate, score
from .stayforlong import ALL_INCLUSIVE, Offer, Stayforlong
from .store import Store
from .tracked import Match, Tracked, nearest
from .weather import WeatherSource
from .wow import WowFeed

log = logging.getLogger("weekends.engine")
TZ = ZoneInfo("Europe/Bratislava")
DATA = Path(__file__).resolve().parents[1] / "data"
SEARCH_FROM_HOUR = 3
MAX_ITEMS = {"weekends": 3, "diamonds": 5, "vacation": 5}
DUE = {"daily": {1, 2, 3, 4, 5, 6, 7}, "3w": {1, 3, 5}, "2w": {1, 4}}


@lru_cache(maxsize=1)
def destinations() -> dict[str, dict]:
    path = DATA / "destinations.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def city_name(iata: str, fallback: str, lang: str) -> str:
    d = destinations().get(iata) or {}
    return (d.get("names") or {}).get(lang) or d.get("city") or fallback


def allowed_countries(s: dict) -> dict[str, str]:
    """Country code -> entry status, for every country the user wants and may enter."""
    wanted = set(s["geography"]["include"])
    for group in s["geography"]["groups"]:
        wanted |= set(GEO["groups"].get(group, []))
    wanted -= set(s["geography"]["exclude"])
    out = {}
    for country in wanted:
        entry = status(s["profile"]["citizenship"], s["profile"]["eu_residence"], country)
        if allowed(entry):
            out[country] = entry
    return out


def _flight_cost(f: Itinerary) -> float:
    """Price plus a penalty for a late outbound or an early return, so a
    flight that keeps the first and the last day wins when it costs a little
    more."""
    late_out = f.depart.hour >= 12
    early_back = 3 <= f.ret_depart.hour < 17
    return f.price + 60 * late_out + 60 * early_back


CHEAPER_BY = 50  # a cheaper but worse-timed flight is mentioned from this saving on
LOST_HOUR_EUR = 20  # half a lost day (7 h) outweighs 140€
SHIFT_DAYS = 3


def holiday_loss(f: Itinerary) -> float:
    """Hours of the holiday spent on the way: landing after 08:00 on the first
    day and taking off before 21:00 on the last one. A night flight loses
    nothing: arriving at 03:30 the family waits for check-in, a 00:25 flight
    home after the checkout day still leaves the whole last day."""
    lost = max(0.0, (f.arrive - datetime.combine(f.arrive.date(), datetime.min.time()).replace(hour=8)).total_seconds() / 3600)
    if f.ret_depart.hour >= 5:
        lost += max(0.0, (datetime.combine(f.ret_depart.date(), datetime.min.time()).replace(hour=21) - f.ret_depart).total_seconds() / 3600)
    return lost


def _holiday_cost(f: Itinerary) -> float:
    return f.price + LOST_HOUR_EUR * holiday_loss(f)


@lru_cache(maxsize=1)
def hotel_facts() -> list[dict]:
    """Resort hotels with their official stars, review score, all inclusive,
    water park and kids' facilities, as Booking.com lists them
    (data/hotel_facts.json, built by scripts/build_hotel_facts.py). These
    facts rarely change; prices for the user's dates come from Xotelo daily."""
    path = DATA / "hotel_facts.json"
    return json.loads(path.read_text()) if path.exists() else []


AQUA_WORDS = ("aqua park", "aquapark", "water park", "waterpark", "waterworld", "water world", "aqua collection", "land of legends")
MAX_SUGGEST = 5
MESSAGE_ROOM = 3800  # Telegram takes 4096 characters
MIN_SUGGEST_REVIEWS = 20


def has_aquapark(h: dict) -> bool:
    name = h["name"].lower()
    return bool(h.get("aqua")) or any(w in name for w in AQUA_WORDS)


def kiwi_url(f: Itinerary) -> str:
    return f.url if f.url.startswith("https://") else "https://www.kiwi.com/"


def stay_dates(f: Itinerary) -> tuple[date, date]:
    """Hotel dates for a holiday flight pair: from the landing day, to the day
    of the flight home, or the day before it when that flight leaves after
    midnight. A night flight never adds a hotel night."""
    out = f.ret_depart.date() - timedelta(days=1) if f.ret_depart.hour < 5 else f.ret_depart.date()
    return f.arrive.date(), out


def party_of(s: dict, mode: str) -> tuple[int, int, int, tuple[int, ...]]:
    """(adults, children 2-11, infants, ages of everyone under 18)."""
    if mode != "family":
        return 1, 0, 0, ()
    return family_party(s["budgets"]["family"])


def family_party(fam: dict) -> tuple[int, int, int, tuple[int, ...]]:
    ages = tuple(fam["children"])
    adults = fam["adults"] + sum(1 for a in ages if a >= 12)
    children = sum(1 for a in ages if 2 <= a <= 11)
    infants = sum(1 for a in ages if a < 2)
    return adults, children, infants, ages


class Engine:
    def __init__(self, cfg: config_module.Config, db: Database, tg: Telegram) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = tg
        self.kiwi = Kiwi(db)
        self.hotels = Hotels(db)
        self.weather = WeatherSource(db)
        self.store = Store(db)
        self.wow = WowFeed()
        self.tracked = Tracked(db)
        self.sfl = Stayforlong(db)
        self.pool: dict[str, dict[str, Itinerary]] = defaultdict(dict)
        self.searched: set[tuple[str, str]] = set()
        self.pool_day: date | None = None

    # ---------------------------------------------------------------- users
    def users(self) -> list[tuple[int, str, dict]]:
        with self.db.tx() as con:
            rows = con.execute(
                "SELECT chat_id, lang, settings FROM users WHERE status = 'active' AND paused = 0 AND settings != ''"
            ).fetchall()
        out = []
        for row in rows:
            try:
                s = json.loads(UserSettings.model_validate_json(row["settings"]).model_dump_json())
            except ValueError:
                continue
            out.append((int(row["chat_id"]), row["lang"], s))
        return out

    # ------------------------------------------------------------- searches
    def ensure_searches(self, users: list[tuple[int, str, dict]], today: date) -> None:
        if self.pool_day != today:
            self.pool.clear()
            self.searched.clear()
            self.pool_day = today
            self.kiwi.purge()
        need: dict[tuple[str, str], dict] = {}
        for _, _, s in users:
            countries = allowed_countries(s)
            cal = UserCalendar(s)
            family = s["budgets"]["family"]["enabled"]
            school = cal.upcoming_school_ranges(today, 150) if family else []
            for origin in s["profile"]["origins"]:
                for country in countries:
                    n = need.setdefault((origin, country), {"horizon": 30, "nmin": 7, "nmax": 1, "school": set()})
                    n["horizon"] = max(n["horizon"], s["trip"]["horizon_days"])
                    n["nmin"] = min(n["nmin"], s["trip"]["nights_min"])
                    n["nmax"] = max(n["nmax"], s["trip"]["nights_max"])
                    n["school"].update(school[:4])
        for pair, n in sorted(need.items()):
            if pair in self.searched:
                continue
            self._search_pair(pair[0], pair[1], n, today)
            self.searched.add(pair)

    def _add(self, items: list[Itinerary]) -> None:
        for it in items:
            known = self.pool[it.origin].get(it.key)
            if known is None or it.price < known.price:
                self.pool[it.origin][it.key] = it
            self.store.record_price(f"{it.origin}-{it.dest}", f"{it.depart:%Y-%m}", it.depart.date().isoformat(), it.price)

    def _search_pair(self, origin: str, country: str, n: dict, today: date) -> None:
        cities = self.store.route_cities(origin, country)
        if cities is None:
            found = self.kiwi.search(country, flyFrom=origin, flyTo=country, departureDate=kdate(today + timedelta(days=1)),
                                     departureDateTo=kdate(today + timedelta(days=180)), nights_in_dst_from=1, nights_in_dst_to=7,
                                     one_for_city=True)
            cities = len({it.dest for it in found})
            self.store.set_route(origin, country, cities)
            self._add(found)
        if not cities:
            return
        bags = {"adults": 1, "adults_hand_bags": [0], "adults_hold_bags": [0]}
        window = {"flyFrom": origin, "flyTo": country, "departureDate": kdate(today + timedelta(days=2)),
                  "departureDateTo": kdate(today + timedelta(days=min(180, n["horizon"])))}
        weekend = {"nights_in_dst_from": 1, "nights_in_dst_to": 4, "fly_days": "4,5,6", "ret_fly_days": "0,1"}
        self._add(self.kiwi.search(country, **window, **bags, **weekend))
        self._add(self.kiwi.search(country, **window, **bags, **weekend, one_for_city=True))
        self._add(self.kiwi.search(country, **window, **bags, nights_in_dst_from=n["nmin"], nights_in_dst_to=min(7, n["nmax"]), one_for_city=True))
        for a, b in sorted(n["school"]):
            start = max(a - timedelta(days=3), today + timedelta(days=2))
            self._add(self.kiwi.search(country, flyFrom=origin, flyTo=country, departureDate=kdate(start), departureDateTo=kdate(b),
                                       nights_in_dst_from=2, nights_in_dst_to=6, one_for_city=True, **bags))

    # -------------------------------------------------------------- digests
    def due_blocks(self, s: dict, weekday: int, first: bool) -> list[str]:
        out = []
        for name in ("weekends", "diamonds", "wow", "vacation"):
            block = s["blocks"][name]
            if not block["enabled"]:
                continue
            days = DUE.get(block["freq"], {s["digest"]["weekly_day"]})
            if first or weekday in days:
                out.append(name)
        return out

    def _family_price(self, it: Itinerary, s: dict) -> float | None:
        adults, children, infants, _ = party_of(s, "family")
        found = self.kiwi.search(it.country, flyFrom=it.origin, flyTo=it.dest, departureDate=kdate(it.depart.date()),
                                 returnDate=kdate(it.ret_depart.date()), adults=adults, children=children, infants=infants,
                                 adults_hand_bags=[0] * adults, adults_hold_bags=[0] * adults,
                                 children_hand_bags=[0] * children or None, children_hold_bags=[0] * children or None)
        for f in found:
            if f.depart == it.depart and f.ret_depart == it.ret_depart:
                return f.price
        return None

    def _stay(self, c: Candidate, s: dict, lang: str, ex: Exclusions, avoid: tuple[str, ...]) -> None:
        adults, _, _, ages = party_of(s, c.mode)
        d = destinations().get(c.it.dest) or {}
        q = StayQuery(city=d.get("city") or c.it.city, country=c.it.country, iata=c.it.dest, lat=d.get("lat"), lon=d.get("lon"),
                      checkin=c.it.checkin, checkout=c.it.checkout, adults=adults if c.mode == "family" else 1,
                      children=tuple(a for a in ages if a < 12) if c.mode == "family" else (),
                      min_rating=s["stay"]["min_rating"], min_reviews=s["stay"]["min_reviews"],
                      breakfast=s["stay"]["breakfast"], min_stars=s["stay"]["min_stars"], lang=lang,
                      exclude=tuple(ex.patterns), avoid=avoid)
        stay = self.hotels.cheapest(q)
        c.hotel_price, c.hotel_name, c.hotel_rating, c.hotel_reviews = stay.price, stay.name, stay.rating, stay.reviews
        c.hotel_url, c.hotel_source, c.hotel_approx, c.hotel_place = stay.url, stay.source, stay.approx, stay.place

    def _weather(self, c: Candidate) -> None:
        d = destinations().get(c.it.dest) or {}
        if d.get("lat") is not None:
            c.weather = self.weather.get(d["lat"], d["lon"], c.it.arrive.date(), c.it.ret_depart.date())

    def pick(self, chat_id: int, s: dict, lang: str, mode: str, today: date) -> list[Candidate]:
        cal = UserCalendar(s)
        countries = allowed_countries(s)
        ex = Exclusions(s["geography"]["exclude_places"])
        family = mode == "family"
        budget = s["budgets"]["family"]["budget_eur"] if family else s["budgets"]["solo"]["budget_eur"]
        ratings = self.store.ratings(chat_id)
        avoid = tuple(k[6:] for k, (v, _) in ratings.items() if k.startswith("hotel:") and v < 0)
        sent = self.store.last_sent(chat_id)
        nights = (s["trip"]["nights_min"], s["trip"]["nights_max"])
        transfer = (50 if family else 30) if s["budgets"]["include_transfer"] else 0
        pool = [it for o in s["profile"]["origins"] for it in self.pool.get(o, {}).values()
                if it.country in countries and not ex.destination(it.dest, city_name(it.dest, it.city, "en"))]
        cands = []
        for it in pool:
            r = evaluate(it, cal, family, nights, today, s["trip"]["horizon_days"])
            if r is None:
                continue
            if mode == "diamond":
                if it.price > s["blocks"]["diamonds"]["max_price_eur"] or r[2] not in ("morning", "evening_before") or r[3] not in ("evening", "before_work"):
                    continue
            elif not family and it.price + transfer > budget:
                continue
            c = Candidate(it=it, mode=mode, flight_price=it.price, entry=countries[it.country],
                          tags=tuple((destinations().get(it.dest) or {}).get("tags", ())), workdays_lost=r[0],
                          school_days_lost=r[1], out_slot=r[2], ret_slot=r[3], transfer=0 if mode == "diamond" else transfer)
            c.usual = self.store.usual_price(f"{it.origin}-{it.dest}", f"{it.depart:%Y-%m}")
            c.penalty = score(c, s, budget if mode != "diamond" else s["blocks"]["diamonds"]["max_price_eur"], ratings)
            cands.append(c)
        cands.sort(key=lambda c: (c.penalty, c.flight_price))
        limit = MAX_ITEMS["diamonds" if mode == "diamond" else "weekends"]
        chosen: list[Candidate] = []
        seen_dest: set[str] = set()
        checks = 0
        for c in cands:
            if len(chosen) >= limit or checks >= 15:
                break
            if c.it.dest in seen_dest:
                continue
            # compared on the one-person fare, which every mode starts from
            prev = sent.get(f"{mode}:{c.it.key}")
            if prev is not None and c.it.price > prev * 0.9:
                continue
            checks += 1
            if family:
                price = self._family_price(c.it, s)
                if price is None:
                    continue
                c.flight_price = price
            if mode != "diamond":
                self._stay(c, s, lang, ex, avoid)
                self._weather(c)
                if c.total > budget:
                    continue
                c.penalty = score(c, s, budget, ratings)
                if c.hotel_price is None:
                    c.penalty += 30  # a trip with a known total beats one without
            seen_dest.add(c.it.dest)
            chosen.append(c)
        chosen.sort(key=lambda c: (c.penalty, c.total))
        return chosen

    def _geos_for(self, country: str) -> list[str]:
        """TripAdvisor areas of the destination cities of one country."""
        keys = tripadvisor_keys()
        return [keys[code] for code, d in destinations().items() if d.get("country", "").lower() == country and code in keys]

    def _tracked_flight(self, s: dict, airport: str, country: str, start: date, end: date, party: tuple[int, int, int]) -> tuple[Itinerary | None, Itinerary | None]:
        """(the flight that keeps most of the holiday, a clearly cheaper one
        if there is one). Kiwi returns at most 15 pairs sorted by price, so
        the well-timed ones are asked for separately: an outbound before noon
        (night flights included) and a return after 16:00."""
        adults, children, infants = party
        found: dict[str, Itinerary] = {}
        for origin in s["profile"]["origins"]:
            for times in ({}, {"dtime_to": 11}, {"ret_dtime_from": 16}):
                for f in self.kiwi.search(country.upper(), flyFrom=origin, flyTo=airport, departureDate=kdate(start), returnDate=kdate(end),
                                          adults=adults, children=children, infants=infants,
                                          adults_hand_bags=[0] * adults, adults_hold_bags=[0] * adults, **times):
                    if f.dest == airport:
                        found.setdefault(f.key, f)
        if not found:
            return None, None
        best = min(found.values(), key=_holiday_cost)
        cheapest = min(found.values(), key=lambda f: f.price)
        return best, (cheapest if cheapest.price <= best.price - CHEAPER_BY else None)

    def _shifted(self, s: dict, m, start: date, end: date, party: tuple[int, int, int], price_of) -> list[tuple[Itinerary, int | None]]:
        """Direct flights a few days around the holiday when the exact dates
        have none or only ones that burn a day: up to two date pairs closest
        to the wanted ones, each with the hotel's price for the new dates."""
        adults, children, infants = party
        country = m.url.split("/hotel/", 1)[-1][:2]
        airport = self._airport(m)
        if not airport:
            return []
        nights = (end - start).days
        found: dict[str, Itinerary] = {}
        for origin in s["profile"]["origins"]:
            for f in self.kiwi.search(country.upper(), flyFrom=origin, flyTo=airport,
                                      departureDate=kdate(start - timedelta(days=SHIFT_DAYS)), departureDateTo=kdate(start + timedelta(days=SHIFT_DAYS)),
                                      returnDate=kdate(end - timedelta(days=SHIFT_DAYS)), returnDateTo=kdate(end + timedelta(days=SHIFT_DAYS)),
                                      nights_in_dst_from=max(1, nights - 2), nights_in_dst_to=nights + 2,
                                      adults=adults, children=children, infants=infants,
                                      adults_hand_bags=[0] * adults, adults_hold_bags=[0] * adults):
                if f.dest == airport and (stay_dates(f) != (start, end)):
                    found.setdefault(f.key, f)
        by_dates: dict[tuple[date, date], Itinerary] = {}
        for f in sorted(found.values(), key=_holiday_cost):
            by_dates.setdefault(stay_dates(f), f)
        ranked = sorted(by_dates.items(), key=lambda kv: (abs((kv[0][0] - start).days) + abs((kv[0][1] - end).days), _holiday_cost(kv[1])))
        out = []
        for (ci, co), f in ranked[:2]:
            out.append((f, price_of(ci, co)))
        return out

    def _airport(self, m) -> str | None:
        country = m.url.split("/hotel/", 1)[-1][:2]
        if m.lat is None:
            return None
        return nearest(m.lat, m.lon, {c: d for c, d in destinations().items() if d.get("country", "").lower() == country})

    def _flight_lines(self, s: dict, lang: str, m, start: date, end: date, party: tuple[int, int, int], guests: int, hotel_total: float,
                      price_of=None) -> list[str]:
        """The flight under a hotel: the pair that keeps most of the holiday,
        a clearly cheaper one, and nearby dates when the exact ones have no
        direct flight or only ones that burn half a day or more."""
        t = dg.TX[lang]
        airport = self._airport(m)
        if not airport:
            return []
        country = m.url.split("/hotel/", 1)[-1][:2]
        lines = []
        f, cheaper = self._tracked_flight(s, airport, country, start, end, party)
        if f:
            lines.append(t["vac_flight"].format(url=dg.e(kiwi_url(f)), air=dg.e(f.airlines), dest=dg.e(f.dest),
                                                o=dg.e(f"{f.depart:%H:%M}"), r=dg.e(f"{f.ret_depart:%H:%M}"), p=round(f.price)))
            if cheaper:
                lines.append(t["vac_cheaper"].format(url=dg.e(kiwi_url(cheaper)), air=dg.e(cheaper.airlines),
                                                     o=dg.e(f"{cheaper.depart:%H:%M}"), r=dg.e(f"{cheaper.ret_depart:%H:%M}"), p=round(cheaper.price)))
            lines.append(t["vac_total"].format(t=round(hotel_total + f.price)))
        else:
            lines.append(t["vac_no_direct"].format(dest=dg.e(airport)))
        if f is None or holiday_loss(f) >= 6:
            if price_of is None:
                def price_of(ci: date, co: date) -> int | None:
                    priced = self.tracked.price(m.key, ci.isoformat(), co.isoformat(), guests, (co - ci).days)
                    return round(priced[0]) if priced else None
            shifted = [(g, p) for g, p in self._shifted(s, m, start, end, party, price_of) if f is None or holiday_loss(g) < holiday_loss(f) / 2]
            if shifted:
                lines.append(t["vac_shift_head"])
            for g, priced in shifted:
                ci, co = stay_dates(g)
                n = (co - ci).days
                line = t["vac_shift"].format(url=dg.e(kiwi_url(g)), a=dg.e(dg.day(lang, ci)), b=dg.e(dg.day(lang, co)), n=f"{n} {dg.plural(lang, n)}",
                                             air=dg.e(g.airlines), o=dg.e(f"{g.depart:%H:%M}"), r=dg.e(f"{g.ret_depart:%H:%M}"), p=round(g.price))
                if priced:
                    delta = round(priced - hotel_total)
                    line += t["vac_shift_hotel"].format(h=priced, d=(f"+{delta}" if delta > 0 else str(delta)), t=round(priced + g.price))
                else:
                    line += t["vac_shift_noprice"]
                lines.append(line)
        return lines

    def _holiday_price(self, key: str, ref: tuple[str, str] | None, lang: str, v: dict, ci: date, co: date, adults: int, ages: tuple[int, ...]
              ) -> tuple[tuple[float, str, float | None] | None, Offer | None, int | None]:
        """(the TripAdvisor price, the Stayforlong rate, the price the total
        is counted with). With free cancellation or paying later asked for,
        the total takes the Stayforlong rate that meets it: TripAdvisor does
        not say how its prices are paid."""
        priced = self.tracked.price(key, ci.isoformat(), co.isoformat(), adults + len(ages), (co - ci).days) if key else None
        offer = self.sfl.best(ref[0], ref[1], lang, ci.isoformat(), co.isoformat(), adults, ages, all_inclusive=v["suggest"]["all_inclusive"],
                              free_cancellation=v.get("free_cancellation", False), pay_later=v.get("pay_later", False)) if ref else None
        strict = v.get("free_cancellation") or v.get("pay_later")
        totals = ([offer.total] if offer else []) + ([round(priced[0])] if priced and not (strict and offer) else [])
        return priced, offer, (min(totals) if totals else None)

    def _price_of(self, key: str, ref: tuple[str, str] | None, lang: str, v: dict, adults: int, ages: tuple[int, ...]):
        """The hotel's price for other dates, for the nearby flights."""
        return lambda ci, co: self._holiday_price(key, ref, lang, v, ci, co, adults, ages)[2]

    def _offer_lines(self, lang: str, v: dict, ref: tuple[str, str] | None, offer: Offer | None) -> list[str]:
        """The Stayforlong rate under a hotel: its total, board and how it is
        paid, or why there is none when conditions were asked for."""
        t = dg.TX[lang]
        if offer is None:
            if ref and (v.get("free_cancellation") or v.get("pay_later")):
                return [t["vac_sfl_none"]]
            return []
        board = t["all_inclusive"] if offer.board in ALL_INCLUSIVE else t.get(f"board_{offer.board.lower()}", "")
        until = dg.e(f"{date.fromisoformat(offer.until):%d.%m}") if offer.until else ""
        if offer.free_cancellation:
            terms = t["vac_sfl_free"].format(d=until) if until else t["vac_sfl_free_open"]
        elif offer.pay_later:
            terms = t["vac_sfl_deposit"].format(n=offer.pay_now, d=until) if until else t["vac_sfl_deposit_open"].format(n=offer.pay_now)
        else:
            terms = t["vac_sfl_now"]
        return [t["vac_sfl"].format(url=dg.e(offer.url), p=offer.total, board=dg.e(f", {board}" if board else ""), terms=terms)]

    def _suggestions(self, s: dict, lang: str, start: date, end: date, party: tuple[int, int, int], ages: tuple[int, ...], ex: Exclusions) -> list[str]:
        """Hotels picked by the user's rules from the hotel facts: countries
        of the holiday, stars, all inclusive, water park, review score, and a
        price for the whole stay within the limit, from TripAdvisor or
        Stayforlong, whichever is lower. Cheapest first, a water park counts
        as a tenth off. Each comes with its flight and the total."""
        v = s["blocks"]["vacation"]
        rule = v["suggest"]
        t = dg.TX[lang]
        adults = party[0]
        nights = (end - start).days
        countries = {c.lower() for c in v["countries"]}
        tracked = set(v["hotels"])
        picked = []
        for h in hotel_facts():
            if h["cc"] not in countries or h["url"] in tracked or not (h.get("ta") or h.get("sfl")):
                continue
            if (h.get("stars") or 0) < rule["min_stars"] or (h.get("score") or 0) < rule["min_rating"] or (h.get("reviews") or 0) < MIN_SUGGEST_REVIEWS:
                continue
            if (rule["all_inclusive"] and not h.get("ai")) or (rule["aquapark"] and not has_aquapark(h)):
                continue
            if ex.hits(h["name"]) or ex.hits(h.get("city", "")):
                continue
            ref = tuple(h["sfl"].split(":", 1)) if h.get("sfl") else None
            priced, offer, total = self._holiday_price(h.get("ta", ""), ref, lang, v, start, end, adults, ages)
            if total is None or total > rule["max_hotel_eur"]:
                continue
            picked.append((total * (0.9 if has_aquapark(h) else 1.0), h, ref, priced, offer, total))
        picked.sort(key=lambda x: x[0])
        if not picked:
            return [t["vac_suggest_none"]]
        booking_q = f"?checkin={start}&checkout={end}&group_adults={adults}&group_children={len(ages)}&no_rooms=1&selected_currency=EUR" + "".join(f"&age={a}" for a in ages)
        lines = [t["vac_suggest_head"]]
        for _, h, ref, priced, offer, total in picked[:MAX_SUGGEST]:
            marks = [f"{h['stars']}★"] + ([t["aquapark"]] if has_aquapark(h) else []) + ([t["all_inclusive"]] if h.get("ai") else [])
            line = t["vac_suggest"].format(url=dg.e(h["url"] + booking_q), name=dg.e(h["name"]), city=dg.e(h.get("city", "")),
                                           marks=dg.e(" · ".join(marks)), score=h["score"], reviews=h["reviews"])
            if priced:
                line += t["vac_price"].format(p=priced[0], n=f"{nights} {dg.plural(lang, nights)}", src=dg.e(priced[1]))
                if priced[2]:
                    line += t["vac_booking"].format(p=priced[2])
            m = Match(url=h["url"], status="found", key=h.get("ta", ""), name=h["name"], lat=h["lat"], lon=h["lon"])
            group = [line, *self._offer_lines(lang, v, ref, offer),
                     *self._flight_lines(s, lang, m, start, end, party, adults + len(ages), total, self._price_of(h.get("ta", ""), ref, lang, v, adults, ages))]
            if len("\n".join(lines + group)) > MESSAGE_ROOM:  # whole hotels only, never a cut tag
                break
            lines += group
        return lines

    def vacation(self, s: dict, lang: str, today: date, full: bool = True, sent: dict[str, float] | None = None) -> tuple[list[str], list[tuple[str, float]]]:
        """The holiday block. full: everything (on the user's day). Otherwise
        only tracked hotels whose price moved by 3% or more since it was last
        sent, or that were never sent, so a price change is never missed."""
        sent = sent or {}
        sent_items: list[tuple[str, float]] = []
        v = s["blocks"]["vacation"]
        t = dg.TX[lang]
        ex = Exclusions(s["geography"]["exclude_places"])
        mode = "family" if v["party"] == "family" else "solo"
        adults, children, infants, ages = family_party(v.get("family") or s["budgets"]["family"]) if mode == "family" else party_of(s, mode)
        guests = adults + len(ages)
        matches = []
        for url in v["hotels"]:
            if ex.hits(url.rsplit("/", 1)[-1].replace("-", " ")):
                continue
            country = url.split("/hotel/", 1)[-1][:2]
            matches.append(self.tracked.resolve(url, v["hotel_names"].get(url, ""), self._geos_for(country)))
        blocks = []
        for opt in v["date_options"]:
            start, end = date.fromisoformat(opt["start"]), date.fromisoformat(opt["end"])
            if start <= today:
                continue
            nights = (end - start).days
            who = (t["family"] + dg.party(lang, adults, len(ages))) if mode == "family" else t["solo"]
            title = f"🌴 <b>{dg.e(t['vacation'])}: {dg.e(t['vac_option'].format(a=dg.day(lang, start), b=dg.day(lang, end), who=who))}</b>"
            lines = [title if full else f"{title}\n<i>{dg.e(t['vac_changed'])}</i>"]
            booking_q = f"?checkin={start}&checkout={end}&group_adults={adults}&group_children={len(ages)}&no_rooms=1&selected_currency=EUR" + "".join(f"&age={a}" for a in ages)
            for m in matches:
                link = dg.e(m.url + booking_q)
                country = m.url.split("/hotel/", 1)[-1][:2]
                slug = m.url.rsplit("/", 1)[-1].removesuffix(".html").replace("-", " ").title()
                name = m.name if m.status == "found" else (v["hotel_names"].get(m.url) or slug)
                ref = self.sfl.resolve(m.url, v["hotel_names"].get(m.url) or name, m.lat, m.lon, country)
                priced, offer, total = self._holiday_price(m.key if m.status == "found" else "", ref, lang, v, start, end, adults, ages)
                rating = t["stay_rating"].format(r=f"{m.rating:.1f}", c=m.reviews) if m.status == "found" else ""
                if total is None:
                    if not full:
                        continue
                    if m.status != "found":
                        lines.append(t["vac_hotel_unknown"].format(url=link, name=dg.e(slug)))
                    else:
                        lines.append(t["vac_hotel_noprice"].format(url=link, name=dg.e(m.name), rating=dg.e(rating)))
                    continue
                price_key = m.key if m.status == "found" else f"sfl{ref[0] if ref else ''}"
                key = f"vac:{price_key}:{start}:{end}:{guests}"
                prev = sent.get(key)
                if not full and prev is not None and abs(total - prev) < prev * 0.03:
                    continue
                sent_items.append((key, float(total)))
                usual = self.store.usual_price(f"hotel:{price_key}:{guests}", f"{start}:{end}")
                self.store.record_price(f"hotel:{price_key}:{guests}", f"{start}:{end}", today.isoformat(), total)
                line = f'🏨 <a href="{link}">{dg.e(name)}</a>' + (f" ({dg.e(rating)})" if rating else "")
                if priced:
                    line += t["vac_price"].format(p=priced[0], n=f"{nights} {dg.plural(lang, nights)}", src=dg.e(priced[1]))
                    if priced[2]:
                        line += t["vac_booking"].format(p=priced[2])
                change = ""
                if prev is not None and abs(total - prev) >= prev * 0.03:
                    change = t["vac_was"].format(p=round(prev))
                elif usual and total < usual * 0.97:
                    change = t["cheaper"].format(x=round(100 * (1 - total / usual)))
                offer_lines = self._offer_lines(lang, v, ref, offer)
                if offer and offer.total == total and offer_lines:
                    offer_lines[-1] += change  # the change belongs to the price the total is counted with
                else:
                    line += change
                lines.append(line)
                lines += offer_lines
                lines += self._flight_lines(s, lang, m, start, end, (adults, children, infants), guests, total,
                                            self._price_of(m.key if m.status == "found" else "", ref, lang, v, adults, ages))
            if not full:
                if len(lines) > 1:
                    blocks.append("\n".join(lines))
                continue
            flights = []
            for origin in s["profile"]["origins"]:
                for country in v["countries"]:
                    for times in ({}, {"dtime_to": 11, "ret_dtime_from": 16}, {"dtime_to": 6, "ret_dtime_from": 16}):  # one per city: the cheapest, the well-timed, the night ones
                        flights += self.kiwi.search(country, flyFrom=origin, flyTo=country, departureDate=kdate(start), returnDate=kdate(end),
                                                    adults=adults, children=children, infants=infants, one_for_city=True,
                                                    adults_hand_bags=[0] * adults, adults_hold_bags=[0] * adults, **times)
            flights = [f for f in flights if not ex.destination(f.dest, city_name(f.dest, f.city, "en"))]
            flights.sort(key=_holiday_cost)
            cheapest: dict[str, Itinerary] = {}
            for f in flights:  # one line per destination: the pair that keeps most of the holiday
                cheapest.setdefault(f.dest, f)
            if cheapest:
                lines.append(t["vac_flights"])
            for f in list(cheapest.values())[:MAX_ITEMS["vacation"]]:
                name = city_name(f.dest, f.city, lang)
                url = f.url if f.url.startswith("https://") else "https://www.kiwi.com/"
                lines.append(f'{dg.flag(f.country)} <a href="{dg.e(url)}">{dg.e(name)}</a> {dg.e(f.dest)}, {dg.e(f.airlines)}: '
                             f"{dg.e(dg.when(lang, f.depart))} / {dg.e(dg.when(lang, f.ret_depart))} · <b>{round(f.price)}€</b>")
            if v["countries"] and not flights:
                lines.append(t["vac_no_flights"])
            blocks.append("\n".join(lines))
            if v["suggest"]["enabled"]:  # a message of its own, it is long with the flights
                blocks.append("\n".join([title] + self._suggestions(s, lang, start, end, (adults, children, infants), ages, ex)))
        return blocks, sent_items

    def build(self, chat_id: int, lang: str, s: dict, today: date, first: bool) -> tuple[list[tuple[str, list[list[dict]] | None]], list[tuple[str, float]]]:
        """One message per trip with its own buttons, like a conversation."""
        t = dg.TX[lang]
        due = self.due_blocks(s, today.isoweekday(), first)
        adults, _, _, ages = party_of(s, "family")
        sections: list[tuple[str, list[Candidate]]] = []
        if "weekends" in due:
            if s["budgets"]["solo"]["enabled"]:
                sections.append((f"{t['weekends']} · {t['solo']}", self.pick(chat_id, s, lang, "solo", today)))
            if s["budgets"]["family"]["enabled"]:
                sections.append((f"{t['weekends']} · {t['family']}", self.pick(chat_id, s, lang, "family", today)))
        if "diamonds" in due:
            sections.append((t["diamonds"], self.pick(chat_id, s, lang, "diamond", today)))
        messages: list[tuple[str, list[list[dict]] | None]] = []
        sent_items: list[tuple[str, float]] = []
        sent_before = self.store.last_sent(chat_id)
        for title, items in sections:
            for i, c in enumerate(items):
                name = city_name(c.it.dest, c.it.city, lang)
                text = dg.item_text(lang, c, name, adults, len(ages), new=f"{c.mode}:{c.it.key}" not in sent_before)
                if i == 0:
                    text = f"<b>━ {dg.e(title)}</b>\n\n{text}"
                aspects = {"country": f"country:{c.it.country}", "city": f"city:{c.it.dest}",
                           "out": f"out:{c.out_slot}", "back": f"back:{c.ret_slot}"}
                ids = {key: secrets.token_hex(6) for key in aspects}
                for key, aspect in aspects.items():
                    self.store.button(ids[key], chat_id, aspect)
                if c.hotel_name:
                    ids["stay"] = secrets.token_hex(6)
                    self.store.button(ids["stay"], chat_id, f"hotel:{c.hotel_name}")
                messages.append((text[:4000], dg.buttons(lang, ids, c.mode)))
                sent_items.append((f"{c.mode}:{c.it.key}", c.it.price))
        if "wow" in due:
            cap = s["blocks"]["wow"]["max_per_person_night"] * 7
            deals = [d for d in self.wow.deals() if f"wow:{d.id}" not in sent_before and (d.price is None or d.price <= cap)][:4]
            if deals:
                lines = [f"<b>━ {dg.e(t['wow'])}</b>"]
                for d in deals:
                    price = f" · {t['from']} {d.price}€" if d.price else ""
                    lines.append(f'🔥 <a href="{dg.e(d.url)}">{dg.e(d.agency)}</a>: {dg.e(d.title)}{price}')
                    sent_items.append((f"wow:{d.id}", float(d.price or 0)))
                messages.append(("\n\n".join(lines)[:4000], None))
        if s["blocks"]["vacation"]["enabled"]:
            # the full block on its day, otherwise only hotel price changes
            blocks, vac_items = self.vacation(s, lang, today, full="vacation" in due, sent=sent_before)
            for block in blocks:
                messages.append((block[:4000], None))
            sent_items += vac_items
        return messages, sent_items

    def send_digest(self, chat_id: int, lang: str, s: dict, today: date, first: bool) -> int:
        messages, sent_items = self.build(chat_id, lang, s, today, first)
        if not messages:
            return 0
        t = dg.TX[lang]
        d = today.strftime("%d.%m.%Y")
        head = f"<b>{dg.e(t['title'].format(d=d))}</b>\n<i>{dg.e(t['head'].format(d=d))}</i>"
        foot = t["footer"].format(url=f"{self.cfg.web_url}?lang={lang}")
        if s["profile"]["citizenship"] is None:
            foot = t["no_citizenship"] + "\n" + foot
        delivered = 0
        for text, keyboard in [(head, None), *messages, (dg.e(foot), None)]:
            if self.tg.send(chat_id, text, keyboard):
                delivered += 1
            time.sleep(0.05)
        if delivered:
            self.store.mark_sent(chat_id, sent_items)
        return len(sent_items)

    # ------------------------------------------------------------------ loop
    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(TZ)
        today = now.date()
        if now.hour < SEARCH_FROM_HOUR:
            return
        users = self.users()
        if not users:
            return
        self.ensure_searches(users, today)
        day = today.isoformat()
        for chat_id, lang, s in users:
            if now.hour < s["digest"]["hour"] or self.store.digest_sent(chat_id, day):
                continue
            first = not self._ever_sent(chat_id)
            try:
                items = self.send_digest(chat_id, lang, s, today, first)
            except Exception:
                log.exception("digest failed for one user")
                continue
            self.store.mark_digest(chat_id, day, items)
        log.info("tick done: users=%d kiwi_calls=%d hotel_calls=%d", len(users), self.kiwi.calls, self.hotels.calls)

    def _ever_sent(self, chat_id: int) -> bool:
        with self.db.tx() as con:
            return con.execute("SELECT 1 FROM digests WHERE chat_id = ? AND items > 0", (chat_id,)).fetchone() is not None


class PrintTelegram:
    """Stand-in used by --dry-run: prints instead of sending."""

    def send(self, chat_id, text, buttons=None):
        print(text)
        print("buttons:", [[b["text"] for b in row] for row in (buttons or [])])
        print("-" * 60)
        return {"message_id": 1}

    def call(self, method, **params):
        return {}


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request would flood the log
    cfg = config_module.load()
    db = Database(cfg.db_path)
    if len(argv) >= 2 and argv[0] == "--dry-run":
        engine = Engine(cfg, db, PrintTelegram())  # type: ignore[arg-type]
        chat_id = int(argv[1])
        users = [u for u in engine.users() if u[0] == chat_id]
        if not users:
            print("no such active configured user")
            return
        today = datetime.now(TZ).date()
        engine.ensure_searches(users, today)
        _, lang, s = users[0]
        engine.send_digest(chat_id, lang, s, today, first=True)
        print("kiwi calls:", engine.kiwi.calls, "hotel calls:", engine.hotels.calls)
        return
    engine = Engine(cfg, db, Telegram(cfg.bot_token, cfg.telegram_ipv4))
    while True:
        try:
            engine.tick()
            engine.store.cleanup()
        except Exception:
            log.exception("tick failed")
        time.sleep(300)


if __name__ == "__main__":
    main(sys.argv[1:])

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
from .hotels import Hotels, StayQuery
from .kiwi import Itinerary, Kiwi, kdate
from .places import Exclusions
from .score import Candidate, evaluate, score
from .store import Store
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


def party_of(s: dict, mode: str) -> tuple[int, int, int, tuple[int, ...]]:
    """(adults, children 2-11, infants, ages of everyone under 18)."""
    if mode != "family":
        return 1, 0, 0, ()
    fam = s["budgets"]["family"]
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

    def vacation(self, s: dict, lang: str, today: date) -> list[str]:
        v = s["blocks"]["vacation"]
        t = dg.TX[lang]
        ex = Exclusions(s["geography"]["exclude_places"])
        mode = "family" if v["party"] == "family" and s["budgets"]["family"]["enabled"] else "solo"
        adults, children, infants, ages = party_of(s, mode)
        blocks = []
        for opt in v["date_options"]:
            start, end = date.fromisoformat(opt["start"]), date.fromisoformat(opt["end"])
            if start <= today:
                continue
            who = (t["family"] + dg.party(lang, adults, len(ages))) if mode == "family" else t["solo"]
            lines = [f"🌴 <b>{dg.e(t['vacation'])}: {dg.e(t['vac_option'].format(a=dg.day(lang, start), b=dg.day(lang, end), who=who))}</b>"]
            flights = []
            for origin in s["profile"]["origins"]:
                for country in v["countries"]:
                    flights += self.kiwi.search(country, flyFrom=origin, flyTo=country, departureDate=kdate(start), returnDate=kdate(end),
                                                adults=adults, children=children, infants=infants, one_for_city=True,
                                                adults_hand_bags=[0] * adults, adults_hold_bags=[0] * adults)
            flights = [f for f in flights if not ex.destination(f.dest, city_name(f.dest, f.city, "en"))]
            flights.sort(key=lambda f: f.price)
            cheapest: dict[str, Itinerary] = {}
            for f in flights:  # one line per destination, from whichever airport is cheaper
                cheapest.setdefault(f.dest, f)
            for f in list(cheapest.values())[:MAX_ITEMS["vacation"]]:
                name = city_name(f.dest, f.city, lang)
                url = f.url if f.url.startswith("https://") else "https://www.kiwi.com/"
                lines.append(f'{dg.flag(f.country)} <a href="{dg.e(url)}">{dg.e(name)}</a> {dg.e(f.dest)}, {dg.e(f.airline)}: '
                             f"{dg.e(dg.when(lang, f.depart))} / {dg.e(dg.when(lang, f.ret_depart))} · <b>{round(f.price)}€</b>")
            if v["countries"] and not flights:
                lines.append(t["vac_no_flights"])
            hotels = [u for u in v["hotels"] if not ex.hits(u.rsplit("/", 1)[-1].replace("-", " "))]
            if hotels:
                lines.append(t["vac_hotels"])
                for url in hotels:
                    slug = url.rsplit("/", 1)[-1].removesuffix(".html").replace("-", " ")
                    params = f"?checkin={start}&checkout={end}&group_adults={adults}&group_children={len(ages)}&no_rooms=1&selected_currency=EUR"
                    params += "".join(f"&age={a}" for a in ages)
                    lines.append(f'🏨 <a href="{dg.e(url + params)}">{dg.e(slug.title())}</a>')
            blocks.append("\n".join(lines))
        return blocks

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
                ids = {"city": secrets.token_hex(6), "times": secrets.token_hex(6)}
                self.store.button(ids["city"], chat_id, f"city:{c.it.dest}")
                self.store.button(ids["times"], chat_id, f"slots:{c.out_slot}:{c.ret_slot}")
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
        if "vacation" in due:
            for block in self.vacation(s, lang, today):
                messages.append((block[:4000], None))
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

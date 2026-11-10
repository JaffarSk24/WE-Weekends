import json
from datetime import date, datetime, timedelta

from conftest import login

from weekends.engine import digest as dg
from weekends.engine.calendar import UserCalendar, outbound_slot, return_slot
from weekends.engine.entry import status
from weekends.engine.kiwi import Itinerary, parse
from weekends.engine.run import Engine, allowed_countries, party_of
from weekends.engine.score import Candidate, evaluate, score
from weekends.models import UserSettings


def settings(**changes):
    s = json.loads(UserSettings().model_dump_json())
    for path, value in changes.items():
        target = s
        keys = path.split("__")
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return s


def itin(depart, back, price=40.0, dest="NAP", country="IT", ret_depart=None):
    depart = datetime.fromisoformat(depart)
    back = datetime.fromisoformat(back)
    ret_depart = datetime.fromisoformat(ret_depart) if ret_depart else back - timedelta(hours=1, minutes=30)
    return Itinerary(origin="BTS", dest=dest, city="Naples", country=country, price=price, depart=depart,
                     arrive=depart + timedelta(hours=2), ret_depart=ret_depart, back=back, airline="Wizz Air",
                     flights="W61/W62", url="https://www.kiwi.com/x", image="")


def test_workdays_lost_rules():
    cal = UserCalendar(settings())
    # Friday 18:00 out, Sunday 22:00 back: nothing lost
    assert cal.workdays_lost(datetime(2026, 11, 13, 18), datetime(2026, 11, 15, 22)) == 0
    # Friday 08:00 out, Monday 11:00 back: Friday and Monday lost
    assert cal.workdays_lost(datetime(2026, 11, 13, 8), datetime(2026, 11, 16, 11)) == 2
    # 24 December is a public holiday in Slovakia: Thursday out early costs nothing
    assert cal.workdays_lost(datetime(2026, 12, 24, 6), datetime(2026, 12, 24, 23)) == 0
    # a custom workweek: Saturday is a workday for this user
    cal6 = UserCalendar(settings(calendar__workdays=[1, 2, 3, 4, 5, 6]))
    assert cal6.workdays_lost(datetime(2026, 11, 14, 8), datetime(2026, 11, 15, 22)) == 1


def test_school_days_by_region():
    s = settings(calendar__school={"mode": "region", "region": "SK-KI", "manual": []})
    cal = UserCalendar(s)
    assert not cal.is_school_day(date(2027, 2, 23))  # Košice spring holidays
    assert cal.is_school_day(date(2027, 3, 2))  # Bratislava's week, school in Košice
    assert UserCalendar(settings(calendar__school={"mode": "region", "region": "SK-BL", "manual": []})).is_school_day(date(2027, 2, 23))


def test_slots():
    cal = UserCalendar(settings())
    assert outbound_slot(datetime(2026, 11, 14, 6, 30), cal) == "morning"
    assert outbound_slot(datetime(2026, 11, 13, 18, 0), cal) == "evening_before"
    assert outbound_slot(datetime(2026, 11, 14, 19, 0), cal) == "day_lost"
    assert return_slot(datetime(2026, 11, 15, 20), datetime(2026, 11, 15, 22), cal) == "evening"
    assert return_slot(datetime(2026, 11, 16, 6), datetime(2026, 11, 16, 7, 30), cal) == "before_work"


def test_entry_rules():
    assert status("RU", "SK", "AT") == "residence"
    assert status("RU", "SK", "GB") == "visa"
    assert status("RU", "SK", "MK") == "residence"
    assert status("RU", "SK", "TR") == "free"
    assert status("RU", "SK", "EG") == "on_arrival"
    assert status("RU", None, "AT") == "visa"
    assert status("SK", None, "GB") in ("eta", "free")
    assert status("SK", None, "FR") == "free"
    assert status(None, None, "GB") == "unknown"


def test_allowed_countries_drop_visa_and_exclusions():
    s = settings(profile__citizenship="RU", profile__eu_residence="SK", geography__groups=["EUROPE"], geography__exclude=["IT"])
    allowed = allowed_countries(s)
    assert "GB" not in allowed and "IE" not in allowed and "IT" not in allowed
    assert allowed["MK"] == "residence" and allowed["ME"] == "free"


def test_party():
    s = settings(budgets__family={"enabled": True, "adults": 2, "children": [8, 1, 13], "budget_eur": 900})
    assert party_of(s, "family") == (3, 1, 1, (8, 1, 13))
    assert party_of(s, "solo") == (1, 0, 0, ())


def test_evaluate_filters():
    today = date(2026, 11, 1)
    cal = UserCalendar(settings(calendar__max_lost_workdays=0, calendar__busy=[{"start": "2026-11-20", "end": "2026-11-22", "scope": "all"}]))
    ok = itin("2026-11-13T18:00", "2026-11-15T22:00")
    assert evaluate(ok, cal, False, (1, 4), today, 90)
    assert evaluate(itin("2026-11-13T08:00", "2026-11-15T22:00"), cal, False, (1, 4), today, 90) is None  # loses Friday
    assert evaluate(itin("2026-11-20T18:00", "2026-11-22T22:00"), cal, False, (1, 4), today, 90) is None  # busy
    assert evaluate(ok, cal, False, (3, 4), today, 90) is None  # too short


def test_score_prefers_sea_and_respects_weights():
    s = settings()
    cal = UserCalendar(s)
    def cand(tags, price):
        it = itin("2026-11-13T18:00", "2026-11-15T22:00", price=price)
        r = evaluate(it, cal, False, (1, 4), date(2026, 11, 1), 90)
        return Candidate(it=it, mode="solo", flight_price=price, entry="free", tags=tags, workdays_lost=r[0],
                         school_days_lost=r[1], out_slot=r[2], ret_slot=r[3])
    sea, city = cand(("sea",), 60), cand(("sights",), 50)
    assert score(sea, s, 300, {}) < score(city, s, 300, {})
    s["rating"]["weights"]["price"] = 10
    s["priorities"]["sea"] = 1
    assert score(sea, s, 300, {}) > score(city, s, 300, {})


def test_parse_drops_connections():
    item = {"price": 50, "bookingUrl": "https://kiwi.com/b", "imageId": "x",
            "outbound": {"stops": 1, "departureTime": "2026-11-13T18:00:00", "arrivalTime": "2026-11-13T20:00:00", "segments": [{"from": "BTS", "to": "NAP", "toCity": "Naples", "carrier": "W6", "flightNumber": "1"}]},
            "inbound": {"stops": 0, "departureTime": "2026-11-15T20:00:00", "arrivalTime": "2026-11-15T22:00:00", "segments": [{"from": "NAP", "to": "BTS", "carrier": "W6", "flightNumber": "2"}]}}
    assert parse(item) is None
    item["outbound"]["stops"] = 0
    it = parse(item)
    assert it and it.dest == "NAP" and it.nights == 2


def test_night_arrival_needs_bed_from_the_evening_before():
    it = itin("2026-11-13T23:30", "2026-11-15T22:00")
    it = Itinerary(**{**it.__dict__, "arrive": datetime(2026, 11, 14, 1, 30)})
    assert it.checkin == date(2026, 11, 13)


def test_digest_escapes_everything():
    it = itin("2026-11-13T18:00", "2026-11-15T22:00")
    it = Itinerary(**{**it.__dict__, "airline": "<b>Evil</b>", "url": 'https://x.example/"><script>'})
    c = Candidate(it=it, mode="solo", flight_price=40, entry="evisa", tags=(), workdays_lost=0, school_days_lost=0,
                  out_slot="evening_before", ret_slot="evening", hotel_url="https://booking.com/?a=1&b=2", transfer=30)
    text = dg.item_text("ru", c, "<i>Napoli</i>")
    assert "<script>" not in text and "<b>Evil</b>" not in text and "<i>Napoli</i>" not in text
    assert "&amp;b=2" in text
    assert "электронная виза" in text


def test_plural():
    assert [dg.plural("ru", n) for n in (1, 2, 5, 11, 21)] == ["ночь", "ночи", "ночей", "ночей", "ночь"]


class FakeKiwi:
    calls = 0

    def __init__(self, items):
        self.items = items

    def search(self, country="", **args):
        self.calls += 1
        out = [Itinerary(**{**i.__dict__, "country": country}) for i in self.items if args.get("flyTo") in (country, i.dest) and i.country in ("", country)]
        if args.get("returnDate"):
            out = [i for i in out if i.depart.strftime("%d/%m/%Y") == args["departureDate"]]
            n = args["adults"] + args.get("children", 0) + args.get("infants", 0)
            return [Itinerary(**{**i.__dict__, "price": i.price * n}) for i in out]
        return out

    def purge(self):
        pass


def test_engine_end_to_end(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["profile"]["citizenship"] = "SK"
    s["geography"]["groups"] = []
    s["geography"]["include"] = ["IT"]
    s["calendar"]["max_lost_workdays"] = 1
    s["budgets"]["family"] = {"enabled": True, "adults": 2, "children": [8], "budget_eur": 500}
    s["blocks"]["vacation"] = {**s["blocks"]["vacation"], "enabled": True, "countries": ["IT"],
                               "date_options": [{"start": "2027-05-24", "end": "2027-06-04"}],
                               "hotels": ["https://www.booking.com/hotel/tr/lonicera-resort-spa.html"]}
    assert client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"}).status_code == 200

    cfg = app.state.cfg
    engine = Engine(cfg, app.state.db, tg)
    fri = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 + 7)
    good = itin(f"{fri}T18:30", f"{fri + timedelta(days=2)}T22:10", price=45, dest="NAP")
    bad = itin(f"{fri}T07:00", f"{fri + timedelta(days=3)}T15:00", price=20, dest="BGY")  # loses two workdays
    vac = itin("2027-05-24T06:00", "2027-06-04T22:00", price=300, dest="BRI")
    engine.kiwi = FakeKiwi([good, bad, vac])

    class FakeTracked:
        def resolve(self, url, alias="", geos=None):
            from weekends.engine.tracked import Match
            if "lonicera" in url:
                return Match(url=url, status="found", key="g1-d2", name="Lonicera Resort & Spa Hotel", rating=4.4, reviews=2500, lat=36.6, lon=31.8)
            return Match(url=url, status="not_found")

        def price(self, key, checkin, checkout, guests, nights):
            return (3531, "Booking.com", None)

    engine.tracked = FakeTracked()
    users = engine.users()
    assert users and users[0][0] == 42
    engine.ensure_searches(users, date.today())
    before = len(tg.sent)
    engine.send_digest(42, "ru", users[0][2], date.today(), first=True)
    text = "\n".join(m[1] for m in tg.sent[before:])
    assert "NAP" in text and "BGY" not in text
    assert "семья" in text and "135€" in text  # family price: 45 x 3 people
    assert "Lonicera Resort &amp; Spa Hotel" in text and "≈3531€" in text and "checkin=2027-05-24" in text
    assert "бронируете вы сами" in text
    buttons = [b for m in tg.sent[before:] for row in (m[2] or []) for b in row]
    assert any(b["callback_data"].startswith("r:") for b in buttons)
    # pressing 👎 stores a rating for that city
    assert [b["text"] for b in buttons[:6]] == ["👎 страна", "👎 город", "👎 жильё", "👎 рейс туда", "👎 рейс обратно", "👍 нравится"] or \
        [b["text"] for b in buttons[:5]] == ["👎 страна", "👎 город", "👎 рейс туда", "👎 рейс обратно", "👍 нравится"]
    data = next(b["callback_data"] for b in buttons if b["text"] == "👎 город")
    app.state.bot.handle({"callback_query": {"id": "1", "data": data, "from": {"id": 42},
                                             "message": {"message_id": 5, "chat": {"id": 42, "type": "private"}}}})
    assert engine.store.ratings(42)["city:NAP"][0] == -1
    # the same trip is not sent twice at the same price
    before = len(tg.sent)
    engine.send_digest(42, "ru", users[0][2], date.today(), first=False)
    assert "NAP" not in "\n".join(m[1] for m in tg.sent[before:])


def test_exclusions_match_whole_words():
    from weekends.engine.places import Exclusions, place_of
    ex = Exclusions(["kemer", "side", "palermo"])
    assert ex.hits("Rixos_Sungate-Kemer_Turkish_Mediterranean_Coast")
    assert not ex.hits("Seaside_Resort-Belek_Serik")
    assert ex.destination("PMO", "Palermo") and not ex.destination("NAP", "Naples")
    assert place_of("Rixos Sungate Kemer Turkish Mediterranean Coast") == "kemer"


def test_excluded_places_are_validated(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["geography"]["exclude_places"] = ["kemer"]
    assert client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    s["geography"]["exclude_places"] = ["atlantis"]
    assert client.put("/v1/settings", json={"version": 1, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"}).status_code == 422


def test_tracked_hotel_matching():
    from weekends.engine.tracked import similarity, slug_words
    country, slug = slug_words("https://www.booking.com/hotel/tr/lonicera-resort-spa.html")
    assert country == "tr"
    assert similarity(slug, "Lonicera Resort & Spa Hotel") >= 0.99
    assert similarity(slug, "Lonicera World Hotel") < 0.75
    assert similarity(slug_words("https://www.booking.com/hotel/tr/letoonia-golf-resort.html")[1], "Selectum Family Resort Belek") == 0
    assert similarity(["selectum", "family", "resort", "belek"], "Selectum Family Resort Belek") >= 0.99


def test_rating_buttons_tick_and_untick(env):
    _, tg, app = env
    bot = app.state.bot
    bot.store.button("aaaaaaaaaaaa", 42, "city:NAP")
    keyboard = [[{"text": "👎 город", "callback_data": "r:aaaaaaaaaaaa:-1"}], [{"text": "👍 нравится", "callback_data": "r:aaaaaaaaaaaa:1"}]]
    def press(data, rows):
        bot.handle({"callback_query": {"id": "1", "data": data, "from": {"id": 42},
                                       "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}, "reply_markup": {"inline_keyboard": rows}}}})
        return next(p["reply_markup"]["inline_keyboard"] for m, p in reversed(tg.calls) if m == "editMessageReplyMarkup")
    rows = press("r:aaaaaaaaaaaa:-1", keyboard)
    assert rows[0][0]["text"] == "✓ 👎 город" and bot.store.ratings(42)["city:NAP"][0] == -1
    rows = press("r:aaaaaaaaaaaa:1", rows)  # the opposite vote replaces the first
    assert rows[0][0]["text"] == "👎 город" and rows[1][0]["text"] == "✓ 👍 нравится" and bot.store.ratings(42)["city:NAP"][0] == 1
    rows = press("r:aaaaaaaaaaaa:1", rows)  # second press takes it back
    assert rows[1][0]["text"] == "👍 нравится" and "city:NAP" not in bot.store.ratings(42)


def test_holiday_updates_only_on_price_change(env):
    from datetime import date
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["blocks"]["vacation"] = {**s["blocks"]["vacation"], "enabled": True, "countries": [],
                               "date_options": [{"start": "2027-05-24", "end": "2027-06-04"}],
                               "hotels": ["https://www.booking.com/hotel/tr/lonicera-resort-spa.html"]}
    client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"})
    engine = Engine(app.state.cfg, app.state.db, tg)
    price = {"v": 3531}

    class T:
        def resolve(self, url, alias="", geos=None):
            from weekends.engine.tracked import Match
            return Match(url=url, status="found", key="g1-d2", name="Lonicera", rating=4.4, reviews=10)

        def price(self, *a):
            return (price["v"], "Booking.com", None)

    engine.tracked = T()
    s = engine.users()[0][2]
    blocks, items = engine.vacation(s, "ru", date(2026, 11, 1), full=False, sent={})
    assert blocks and items  # never sent before: goes out
    sent = dict(items)
    assert engine.vacation(s, "ru", date(2026, 11, 2), full=False, sent=sent)[0] == []  # same price: quiet
    price["v"] = 3300
    blocks, _ = engine.vacation(s, "ru", date(2026, 11, 3), full=False, sent=sent)
    assert blocks and "было 3531€" in blocks[0]


def test_holiday_other_flights_keep_the_first_and_last_day(env):
    from datetime import date
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["blocks"]["vacation"] = {**s["blocks"]["vacation"], "enabled": True, "countries": ["TR"], "hotels": [],
                               "date_options": [{"start": "2027-05-27", "end": "2027-06-06"}]}
    client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"})
    late = itin("2027-05-27T10:50", "2027-06-06T23:10", price=778, dest="AYT", country="TR", ret_depart="2027-06-06T21:40")
    late = Itinerary(**{**late.__dict__, "arrive": datetime(2027, 5, 27, 14, 5), "airline": "Pegasus"})
    night = itin("2027-05-27T00:25", "2027-06-06T23:10", price=781, dest="AYT", country="TR", ret_depart="2027-06-06T21:40")
    night = Itinerary(**{**night.__dict__, "arrive": datetime(2027, 5, 27, 3, 40), "airline": "SunExpress"})

    class K:
        def search(self, country="", **args):
            # one result per city, the cheapest one in the asked window: 10:50 still fits "before 11"
            return [night] if args.get("dtime_to") == 6 else [late]

    engine = Engine(app.state.cfg, app.state.db, tg)
    engine.kiwi = K()
    blocks, _ = engine.vacation(engine.users()[0][2], "ru", date(2026, 11, 2), full=True, sent={})
    assert "SunExpress" in blocks[0] and "Pegasus" not in blocks[0]


class RouteKiwi:
    """Answers exact-date searches with `exact` and range searches with `shifted`."""

    def __init__(self, exact, shifted=()):
        self.exact, self.shifted = list(exact), list(shifted)

    def search(self, country="", **args):
        pool = self.shifted if args.get("departureDateTo") else self.exact
        out = [i for i in pool if i.dest == args.get("flyTo")]
        if "dtime_to" in args:
            out = [i for i in out if i.depart.hour <= args["dtime_to"]]
        if "ret_dtime_from" in args:
            out = [i for i in out if i.ret_depart.hour >= args["ret_dtime_from"]]
        return out


class PriceTracked:
    def __init__(self, prices):
        self.prices = prices

    def price(self, key, checkin, checkout, guests, nights):
        p = self.prices.get((key, checkin, checkout))
        return (p, "Booking.com", None) if p else None


def holiday_engine(kiwi, tracked=None, sfl=None):
    from weekends.engine import run
    engine = Engine.__new__(Engine)
    engine.kiwi, engine.tracked, engine.sfl = kiwi, tracked or PriceTracked({}), sfl or run.Stayforlong(None)
    return engine


def test_holiday_flight_keeps_the_first_and_last_day():
    from weekends.engine.run import holiday_loss, stay_dates
    late = itin("2027-05-27T10:50", "2027-06-06T23:10", price=778, dest="AYT", ret_depart="2027-06-06T21:40")
    late = Itinerary(**{**late.__dict__, "arrive": datetime(2027, 5, 27, 14, 5), "airline": "Pegasus"})
    night = itin("2027-05-27T00:25", "2027-06-06T23:10", price=781, dest="AYT", ret_depart="2027-06-06T21:40")
    night = Itinerary(**{**night.__dict__, "arrive": datetime(2027, 5, 27, 3, 40), "airline": "SunExpress"})
    early_back = itin("2027-05-27T00:25", "2027-06-06T10:30", price=600, dest="AYT", ret_depart="2027-06-06T09:00")
    early_back = Itinerary(**{**early_back.__dict__, "arrive": datetime(2027, 5, 27, 3, 40)})
    assert holiday_loss(night) == 0 and holiday_loss(late) > 6
    assert stay_dates(night) == (date(2027, 5, 27), date(2027, 6, 6))
    after_midnight = Itinerary(**{**night.__dict__, "ret_depart": datetime(2027, 6, 7, 0, 20)})
    assert stay_dates(after_midnight) == (date(2027, 5, 27), date(2027, 6, 6)) and holiday_loss(after_midnight) == 0
    engine = holiday_engine(RouteKiwi([late, night, early_back]))
    best, cheaper = engine._tracked_flight({"profile": {"origins": ["BTS"]}}, "AYT", "tr", date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0))
    assert best.airline == "SunExpress" and best.ret_depart.hour == 21
    assert cheaper is early_back  # 181€ cheaper but loses the last day: mentioned, not chosen


def test_holiday_without_direct_flights_offers_nearby_dates():
    from weekends.engine.tracked import Match
    shifted = itin("2027-05-26T11:25", "2027-06-06T12:30", price=601, dest="CFU", country="GR", ret_depart="2027-06-06T20:55")
    m = Match(url="https://www.booking.com/hotel/gr/canvas-by-mitsis-messonghi.html", status="found", key="g1-d2", name="Canvas", lat=39.48, lon=19.93)
    tracked = PriceTracked({("g1-d2", "2026-05-26", "2027-06-06"): 1, ("g1-d2", "2027-05-26", "2027-06-06"): 2277})
    engine = holiday_engine(RouteKiwi([], [shifted]), tracked)
    lines = engine._flight_lines({"profile": {"origins": ["BTS"]}}, "ru", m, date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0), 3, 2080)
    text = "\n".join(lines)
    assert "прямых рейсов в CFU" in text and "сдвинуть даты" in text
    assert "26.05" in text and "601€" in text and "≈2277€ (+197€" in text and "≈2878€" in text


def test_hotel_suggestions_follow_the_rules(monkeypatch):
    from weekends.engine import run
    from weekends.engine.places import Exclusions
    facts = [
        {"url": "https://www.booking.com/hotel/tr/a.html", "name": "Aqua Resort", "cc": "tr", "city": "Belek", "lat": 36.85, "lon": 31.05,
         "stars": 5, "score": 8.8, "reviews": 900, "ai": True, "aqua": True, "ta": "g1-d1"},
        {"url": "https://www.booking.com/hotel/tr/b.html", "name": "Plain Resort", "cc": "tr", "city": "Belek", "lat": 36.85, "lon": 31.05,
         "stars": 5, "score": 8.9, "reviews": 900, "ai": True, "aqua": False, "ta": "g1-d2"},
        {"url": "https://www.booking.com/hotel/tr/c.html", "name": "Four Star", "cc": "tr", "city": "Side", "lat": 36.77, "lon": 31.39,
         "stars": 4, "score": 9.0, "reviews": 900, "ai": True, "aqua": True, "ta": "g1-d3"},
        {"url": "https://www.booking.com/hotel/tr/d.html", "name": "Kemer Palace", "cc": "tr", "city": "Kemer", "lat": 36.6, "lon": 30.55,
         "stars": 5, "score": 9.0, "reviews": 900, "ai": True, "aqua": True, "ta": "g1-d4"},
    ]
    monkeypatch.setattr(run, "hotel_facts", lambda: facts)
    prices = {(h["ta"], "2027-05-27", "2027-06-06"): 2500 for h in facts}
    engine = holiday_engine(RouteKiwi([]), PriceTracked(prices))
    s = settings(blocks__vacation__countries=["TR"], blocks__vacation__suggest={"enabled": True, "max_hotel_eur": 3000, "min_stars": 5,
                                                                                "all_inclusive": True, "aquapark": True, "min_rating": 8.5})
    lines = engine._suggestions(s, "ru", date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0), (8,), Exclusions(["kemer"]))
    text = "\n".join(lines)
    assert "Aqua Resort" in text and "5★ · аквапарк · всё включено" in text and "≈2500€" in text and "checkin=2027-05-27" in text
    assert "Plain Resort" not in text and "Four Star" not in text and "Kemer Palace" not in text
    assert "первой линии" in text
    s["blocks"]["vacation"]["suggest"]["max_hotel_eur"] = 2000
    assert engine._suggestions(s, "ru", date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0), (8,), Exclusions([]))[0].startswith("✨ Под ваши")


def sfl_rate(total, board="AI", policy="NOR", pay_now=0, until="2027-05-15", prime=False):
    return {"summary": {"total_with_fees": {"amount": total}}, "board_group_code": board, "payment_policy": policy, "pay_rate": pay_now,
            "cancellation_policies": [{"amount": pay_now, "from": until}] if policy != "NRF" else [], "is_prime": prime, "currency": "EUR", "rooms": 1}


def sfl_client(handler):
    import httpx
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_stayforlong_rates_follow_the_conditions(tmp_path, monkeypatch):
    import httpx

    from weekends.db import Database
    from weekends.engine import stayforlong
    monkeypatch.setattr(stayforlong, "PAUSE", 0)
    asked = []

    def handler(request):
        asked.append(request.url)
        return httpx.Response(200, json={"40494": {"availability": [
            sfl_rate(2260), sfl_rate(2190, policy="SFL", pay_now=114), sfl_rate(2000, board="RO", policy="NRF", pay_now=2000),
            sfl_rate(1500, prime=True)]}})

    sfl = stayforlong.Stayforlong(Database(str(tmp_path / "s.sqlite")), sfl_client(handler))
    args = ("40494", "tr/selectum-family-resort-belek_serik", "ru", "2027-05-27", "2027-06-06", 2, (8,))
    best = sfl.best(*args, all_inclusive=True)
    assert (best.total, best.policy, best.pay_now) == (2190, "SFL", 114)  # Prime needs a membership, room only is not all inclusive
    assert "childrenAges=8" in str(asked[0]) and best.url.endswith("checkIn=2027-05-27&checkOut=2027-06-06&children=8")
    assert sfl.best(*args, all_inclusive=True, free_cancellation=True).total == 2260
    assert sfl.best(*args, all_inclusive=True, pay_later=True).total == 2190
    assert sfl.best(*args).total == 2000
    assert len(asked) == 1  # the rates are cached for all users with the same stay


def test_stayforlong_search_finds_the_same_hotel(tmp_path, monkeypatch):
    import httpx

    from weekends.db import Database
    from weekends.engine import stayforlong
    monkeypatch.setattr(stayforlong, "PAUSE", 0)
    asteria = {"ID": "40523", "Slug": "asteria-family-resort-belek", "CitySlug": "serik", "Country": "tr", "Type": "hotel",
               "Text": "Asteria Family Resort Belek", "Coordinate": {"lat": 36.851, "lon": 31.051}}
    selectum = {**asteria, "ID": "40494", "Slug": "selectum-family-resort-belek", "Text": "Selectum Family Resort Belek"}
    hits = {"v": [asteria, selectum]}
    sfl = stayforlong.Stayforlong(Database(str(tmp_path / "s.sqlite")), sfl_client(lambda r: httpx.Response(200, json=hits["v"])))
    assert sfl.search("Selectum Family Resort Belek", 36.85, 31.05, "tr") == ("40494", "tr/selectum-family-resort-belek_serik")
    hits["v"] = [asteria]
    assert sfl.search("Selectum Family Resort Belek", 36.85, 31.05, "tr") is None
    assert sfl.search("Selectum Family Resort Belek", 36.85, 31.05, "gr") is None


class OfferSfl:
    def __init__(self, offers):
        self.offers = offers

    def resolve(self, *a, **kw):
        return ("40494", "tr/x_serik")

    def best(self, sfl_id, path, lang, checkin, checkout, adults, ages, all_inclusive=False, free_cancellation=False, pay_later=False):
        ok = [o for o in self.offers if (not free_cancellation or o.free_cancellation) and (not pay_later or o.pay_later)]
        return min(ok, key=lambda o: o.total) if ok else None


def test_holiday_counts_the_stayforlong_price(monkeypatch):
    from weekends.engine import run
    from weekends.engine.places import Exclusions
    from weekends.engine.stayforlong import Offer
    facts = [{"url": "https://www.booking.com/hotel/tr/a.html", "name": "Aqua Resort", "cc": "tr", "city": "Belek", "lat": 36.85, "lon": 31.05,
              "stars": 5, "score": 8.8, "reviews": 900, "ai": True, "aqua": True, "ta": "g1-d1", "sfl": "40494:tr/a_serik"},
             {"url": "https://www.booking.com/hotel/tr/b.html", "name": "Only There", "cc": "tr", "city": "Side", "lat": 36.77, "lon": 31.39,
              "stars": 5, "score": 8.9, "reviews": 900, "ai": True, "aqua": True, "sfl": "40495:tr/b_side"}]
    monkeypatch.setattr(run, "hotel_facts", lambda: facts)
    deposit = Offer(total=2190, board="AI", policy="SFL", pay_now=114, until="2027-05-15", url="https://www.stayforlong.com/sk-sk/hotel/tr/a_serik")
    engine = holiday_engine(RouteKiwi([]), PriceTracked({("g1-d1", "2027-05-27", "2027-06-06"): 4200}), OfferSfl([deposit]))
    s = settings(blocks__vacation__countries=["TR"], blocks__vacation__suggest={"enabled": True, "max_hotel_eur": 3000, "min_stars": 5,
                                                                                "all_inclusive": True, "aquapark": True, "min_rating": 8.5})
    text = "\n".join(engine._suggestions(s, "ru", date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0), (8,), Exclusions([])))
    # 4200€ on TripAdvisor is over the limit, 2190€ on Stayforlong is not; a hotel TripAdvisor has no prices for gets in too
    assert "Aqua Resort" in text and "≈4200€" in text and "Stayforlong</a> ≈2190€, всё включено · сейчас 114€ без возврата, остальное до 15.05" in text
    assert "Only There" in text
    s["blocks"]["vacation"]["free_cancellation"] = True
    s["blocks"]["vacation"]["suggest"]["max_hotel_eur"] = 5000
    text = "\n".join(engine._suggestions(s, "ru", date(2027, 5, 27), date(2027, 6, 6), (2, 1, 0), (8,), Exclusions([])))
    assert "тарифов на ваших условиях" in text and "Only There" not in text  # no free cancellation: only the TripAdvisor price is left


def test_holiday_price_change_is_marked_on_the_counted_price(env):
    from datetime import date

    from weekends.engine.stayforlong import Offer
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["blocks"]["vacation"] = {**s["blocks"]["vacation"], "enabled": True, "countries": [],
                               "date_options": [{"start": "2027-05-27", "end": "2027-06-06"}],
                               "hotels": ["https://www.booking.com/hotel/tr/lonicera-resort-spa.html"]}
    client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"})
    engine = Engine(app.state.cfg, app.state.db, tg)

    class T:
        def resolve(self, url, alias="", geos=None):
            from weekends.engine.tracked import Match
            return Match(url=url, status="found", key="g1-d2", name="Lonicera", rating=4.4, reviews=10)

        def price(self, *a):
            return (3210, "Booking.com", None)

    engine.tracked = T()
    engine.sfl = OfferSfl([Offer(total=1480, board="AI", policy="SFL", pay_now=73, until="2027-05-14", url="https://www.stayforlong.com/x")])
    key = engine.vacation(engine.users()[0][2], "ru", date(2026, 11, 4), full=False, sent={})[1][0][0]
    blocks, items = engine.vacation(engine.users()[0][2], "ru", date(2026, 11, 4), full=False, sent={key: 3300.0})
    hotel, sfl = blocks[0].split("\n")[2:4]
    assert "≈3210€" in hotel and "было" not in hotel
    assert "≈1480€" in sfl and sfl.endswith("(было 3300€)") and dict(items)[key] == 1480


def test_stayforlong_asks_for_no_children_and_keeps_no_odd_answer(tmp_path, monkeypatch):
    import httpx

    from weekends.db import Database
    from weekends.engine import stayforlong
    monkeypatch.setattr(stayforlong, "PAUSE", 0)
    asked = []

    def handler(request):
        asked.append(request.url)
        if "childrenByRoom" not in str(request.url):
            return httpx.Response(200, json={"defaults": {}})  # what the gateway answers without it
        return httpx.Response(200, json={"40494": {"availability": [sfl_rate(1750)]}})

    sfl = stayforlong.Stayforlong(Database(str(tmp_path / "s.sqlite")), sfl_client(handler))
    assert sfl.best("40494", "tr/x_serik", "en", "2027-05-27", "2027-06-06", 1, ()).total == 1750
    assert "childrenByRoom=0" in str(asked[0]) and "childrenAges" not in str(asked[0])
    monkeypatch.setattr(stayforlong, "GATEWAY", "https://gw.example")
    odd = stayforlong.Stayforlong(Database(str(tmp_path / "o.sqlite")), sfl_client(lambda r: httpx.Response(200, json={"defaults": {}})))
    assert odd.offers("1", "2027-05-27", "2027-06-06", 1, ()) == []
    with odd.db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM sfl_cache").fetchone()[0] == 0


def test_holiday_has_its_own_family(env):
    from datetime import date

    from weekends.models import UserSettings
    old = UserSettings.model_validate({"budgets": {"family": {"enabled": False, "adults": 2, "children": [8]}}})
    assert old.blocks.vacation.family.adults == 2 and old.blocks.vacation.family.children == [8]  # taken over once
    client, tg, app = env
    token = login(client, app, tg, 42)
    s = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["settings"]
    s["budgets"]["family"] = {**s["budgets"]["family"], "enabled": False, "adults": 1, "children": []}
    s["blocks"]["vacation"] = {**s["blocks"]["vacation"], "enabled": True, "party": "family", "family": {"adults": 3, "children": [8, 1]},
                               "countries": [], "date_options": [{"start": "2027-05-27", "end": "2027-06-06"}],
                               "hotels": ["https://www.booking.com/hotel/tr/lonicera-resort-spa.html"]}
    assert client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": s}, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    engine = Engine(app.state.cfg, app.state.db, tg)
    asked = []

    class T:
        def resolve(self, url, alias="", geos=None):
            from weekends.engine.tracked import Match
            return Match(url=url, status="found", key="g1-d2", name="Lonicera", rating=4.4, reviews=10)

        def price(self, key, checkin, checkout, guests, nights):
            asked.append(guests)
            return (3000, "Booking.com", None)

    engine.tracked = T()
    blocks, _ = engine.vacation(engine.users()[0][2], "ru", date(2026, 11, 5), full=False, sent={})
    assert "3 взр. + 2 реб." in blocks[0] and asked == [5]  # its own family, family weekends off


def stay_query(**kw):
    from weekends.engine.hotels import StayQuery
    base = dict(city="Naples", country="IT", iata="NAP", lat=40.85, lon=14.27, checkin=date(2026, 12, 11), checkout=date(2026, 12, 13),
                adults=2, children=(8,), min_rating=8.0, min_reviews=100, breakfast="any", min_stars=0, lang="ru")
    return StayQuery(**{**base, **kw})


def test_xotelo_prices_hotels_without_a_price_hint(tmp_path):
    import httpx
    from conftest import REAL_XOTELO

    from weekends.db import Database
    from weekends.engine.hotels import Hotels
    rates = {"h1": [], "h2": [{"code": "Agoda", "rate": 100}], "h3": [{"code": "BookingCom", "rate": 80}], "h4": [{"code": "Agoda", "rate": 90}],
             "h5": [{"code": "Agoda", "rate": 10}]}
    asked = []

    def handler(request):
        if request.url.path.endswith("/list"):  # as Xotelo answers now: no price_ranges at all
            return httpx.Response(200, json={"result": {"list": [
                {"key": k, "name": f"Hotel {k}", "accommodation_type": "Hotel", "review_summary": {"rating": 4.5, "count": 900},
                 "price_ranges": None, "url": f"https://www.tripadvisor.com/Hotel_Review-g1-d1-Reviews-Hotel_{k}-Naples.html"} for k in rates]}})
        asked.append(request.url.params["hotel_key"])
        return httpx.Response(200, json={"result": {"rates": rates[request.url.params["hotel_key"]]}})

    hotels = Hotels(Database(str(tmp_path / "h.sqlite")))
    hotels.client = httpx.Client(transport=httpx.MockTransport(handler))
    stay = REAL_XOTELO(hotels, stay_query(), "g1")
    assert (stay.name, stay.price, stay.source) == ("Hotel h5", 20, "xotelo")
    assert sorted(asked) == ["h1", "h2", "h3", "h4", "h5"]  # asked together


def test_stay_always_gets_a_price(tmp_path, monkeypatch):
    from weekends.db import Database
    from weekends.engine import hotels as hm
    from weekends.engine.hotels import Hotels, Stay
    q = stay_query()

    class Around:
        def __init__(self, stay):
            self.stay = stay

        def around(self, q):
            return self.stay

    found = Stay(name="Sea View", price=420, rating=8.7, reviews=60, url="https://www.stayforlong.com/x", source="stayforlong")
    assert Hotels(Database(str(tmp_path / "a.sqlite")), Around(found)).cheapest(q) is found  # Xotelo has nothing: Stayforlong
    monkeypatch.setattr(hm, "stay_baseline", lambda: {"cities": {"NAP": 100}, "countries": {"IT": 90}, "all": 80})
    guess = Hotels(Database(str(tmp_path / "b.sqlite")), Around(None)).cheapest(q)
    assert (guess.source, guess.price, guess.approx) == ("estimate", 270, True) and "booking.com" in guess.url  # 100 a night, a third more for the child
    assert hm.estimate(stay_query(iata="XXX", adults=1, children=())) == 153  # the country's price, most of a double for one
    assert hm.estimate(stay_query(iata="XXX", country="ZZ")) == 216
    monkeypatch.setattr(hm, "stay_baseline", lambda: {})
    assert Hotels(Database(str(tmp_path / "c.sqlite")), Around(None)).cheapest(q).price is None


def test_stayforlong_around_a_city(tmp_path, monkeypatch):
    import httpx

    from weekends.db import Database
    from weekends.engine import stayforlong
    monkeypatch.setattr(stayforlong, "PAUSE", 0)
    asked = []

    def prop(name, total, rating, comments, room="Double room"):
        return {"name": name, "selling_rate": total, "currency": "EUR", "rating": rating, "comments": comments, "slug": name.lower().replace(" ", "-"),
                "rooms": [{"room_name": room}], "property_location": {"city": "Naples", "city_slug": "naples", "country_code": "it"}}

    def handler(request):
        asked.append(json.loads(request.content))
        return httpx.Response(200, json={"metadata": {}, "properties": [
            prop("Bunk House", 60, 9.5, 500, "Bed in 10-bed dorm"), prop("Few Reviews", 150, 9.0, 5), prop("Low Score", 160, 7.0, 400),
            prop("Old Town Kemer", 170, 9.0, 400), prop("Sea View", 420, 8.7, 60), prop("Grand", 500, 9.1, 900)]})

    sfl = stayforlong.Stayforlong(Database(str(tmp_path / "s.sqlite")), sfl_client(handler))
    from weekends.engine.places import Exclusions
    stay = sfl.around(stay_query(exclude=tuple(Exclusions(["kemer"]).patterns)))
    assert (stay.name, stay.price, stay.source, stay.reviews) == ("Sea View", 420, "stayforlong", 60)
    assert stay.url.startswith("https://www.stayforlong.com/sk-sk/hotel/it/sea-view_naples?adults=2&checkIn=2026-12-11")
    body = asked[0]
    assert body["rooms"] == [{"adults": 2, "children": [8]}] and body["rating"] == 8 and body["accommodations"] == ["HOTEL"]
    assert sfl.around(stay_query(exclude=tuple(Exclusions(["kemer"]).patterns))).price == 420 and len(asked) == 1  # cached


def test_estimated_stay_is_said_to_be_one():
    from weekends.engine.digest import item_text
    from weekends.engine.score import Candidate
    it = itin("2026-12-11T18:30", "2026-12-13T22:10", price=45, dest="NAP")
    c = Candidate(it=it, mode="solo", flight_price=45, entry="free", tags=[], workdays_lost=0, school_days_lost=0, out_slot="evening", ret_slot="evening",
                  hotel_price=170, hotel_url="https://www.booking.com/searchresults.html?ss=Naples",
                  hotel_source="estimate", hotel_approx=True)
    text = item_text("ru", c, "Неаполь")
    assert "жильё ≈170€ за 2 ночи" in text and "обычная цена" in text and "Итого ≈215€</b> с оценкой жилья" in text
    assert "не нашли" not in text


def test_owner_hears_when_a_source_goes_quiet(env):
    from conftest import OWNER
    client, tg, app = env
    engine = Engine(app.state.cfg, app.state.db, tg)
    engine.pool["BTS"]["k"] = itin("2026-12-11T18:30", "2026-12-13T22:10")
    engine.stays.update({"xotelo": 5, "estimate": 3})
    engine._watch([(OWNER, "ru", {})], "2026-11-10")
    assert not tg.sent  # most stays have a real price
    engine.stays.update({"xotelo": 1, "estimate": 9})
    engine._watch([(OWNER, "ru", {})], "2026-11-10")
    engine._watch([(OWNER, "ru", {})], "2026-11-10")
    assert len(tg.sent) == 1 and tg.sent[0][0] == OWNER and "только для 1 из 10" in tg.sent[0][1]  # once a day
    engine.pool.clear()
    engine.searched.add(("BTS", "IT"))
    engine._watch([(OWNER, "ru", {})], "2026-11-11")
    assert "поиск рейсов" in tg.sent[1][1]

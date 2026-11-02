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


def holiday_engine(kiwi, tracked=None):
    engine = Engine.__new__(Engine)
    engine.kiwi, engine.tracked = kiwi, tracked or PriceTracked({})
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

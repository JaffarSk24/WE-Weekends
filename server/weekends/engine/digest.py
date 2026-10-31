"""The messages. One message per trip, with its own rating buttons; a short
note on top and the disclaimer at the end. Everything taken from outside
(city and hotel names, airlines, links) is escaped for Telegram HTML."""
from __future__ import annotations

import html
import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from ..models import GEO
from .score import Candidate

DATA = Path(__file__).resolve().parents[1] / "data"

TX = {
    "ru": {
        "title": "WE Weekends · {d}",
        "head": "Цены Kiwi и отелей на {d}. Итого включает билеты туда и обратно для всех, жильё на все ночи и резерв на трансфер (оценка). У Ryanair и Wizz Air напрямую те же рейсы часто дешевле, сравните перед покупкой.",
        "weekends": "Выходные", "solo": "один", "family": "семья", "diamonds": "Бриллианты: билеты без отеля",
        "vacation": "Отпуск", "wow": "Горящие пакеты", "from": "от",
        "nights": "ночей: {n}",
        "flight": "🛫 <a href=\"{url}\">{air}</a> {dest}, туда {o}, обратно {r} · {p}€{who}",
        "who_family": " на всех",
        "stay": "🏨 <a href=\"{url}\">{name}</a>{place} ({rating}) · {p}€ за {n}",
        "stay_rating": "TripAdvisor {r} из 5, отзывов: {c}",
        "stay_nights": {1: "ночь", 2: "ночи", 5: "ночей"},
        "stay_none": "🏨 цену жилья не нашли: <a href=\"{url}\">варианты на Booking</a> на ваши даты",
        "transfer": "🚕 трансфер туда и обратно ~{x}€ (оценка)",
        "total": "💶 <b>Итого {t}€</b>",
        "total_no_stay": "💶 <b>Билеты и трансфер {t}€</b>, жильё сверху",
        "total_diamond": "💶 <b>{t}€ за оба билета</b>",
        "party": " на {a} взр.{k}",
        "party_kids": " + {n} реб.",
        "new": " · новое", "cheaper": " · дешевле обычного на {x}%",
        "weather_f": "🌤 прогноз: до {t}°C, дождливых дней {r} из {n}",
        "weather_y": "🌡 в эти дни год назад: до {t}°C, дождливых дней {r} из {n}",
        "entry": {"evisa": "🛂 нужна электронная виза", "on_arrival": "🛂 виза по прилёте", "eta": "🛂 нужна электронная регистрация заранее", "residence": "🛂 без визы по виду на жительство в ЕС", "unknown": ""},
        "lost": "Рабочих дней теряется: {n}",
        "school": "учебных дней пропускает ребёнок: {n}",
        "slots": {"morning": "вылет рано утром", "evening_before": "вылет вечером после работы", "midday": "вылет днём", "day_lost": "вылет поздно, день уходит на дорогу",
                  "before_work": "прилёт утром прямо к работе", "evening": "обратно вечером", "afternoon": "обратно днём", "ret_day_lost": "обратно утром, последний день уходит на дорогу"},
        "btn_country": "👎 страна", "btn_city": "👎 город", "btn_stay": "👎 жильё", "btn_out": "👎 рейс туда", "btn_back": "👎 рейс обратно", "btn_up": "👍 нравится",
        "vac_option": "{a} - {b}, {who}", "vac_hotels": "Ваши отели на эти даты:", "vac_no_flights": "прямых рейсов на эти даты пока нет",
        "tickets": "билеты",
        "footer": "Это только рекомендации: бронируете вы сами, цены могут измениться. Настройки: {url}",
        "no_citizenship": "Укажите гражданство в настройках, чтобы не видеть стран, куда нужна виза.",
        "days": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    },
    "sk": {
        "title": "WE Weekends · {d}",
        "head": "Ceny Kiwi a hotelov k {d}. Spolu zahŕňa letenky tam a späť pre všetkých, ubytovanie na všetky noci a rezervu na transfer (odhad). Priamo u Ryanair a Wizz Air sú tie isté lety často lacnejšie, porovnajte pred kúpou.",
        "weekends": "Víkendy", "solo": "sólo", "family": "rodina", "diamonds": "Diamanty: letenky bez hotela",
        "vacation": "Dovolenka", "wow": "Last minute balíky", "from": "od",
        "nights": "nocí: {n}",
        "flight": "🛫 <a href=\"{url}\">{air}</a> {dest}, tam {o}, späť {r} · {p}€{who}",
        "who_family": " pre všetkých",
        "stay": "🏨 <a href=\"{url}\">{name}</a>{place} ({rating}) · {p}€ za {n}",
        "stay_rating": "TripAdvisor {r} z 5, recenzií: {c}",
        "stay_nights": {1: "noc", 2: "noci", 5: "nocí"},
        "stay_none": "🏨 cenu ubytovania sme nenašli: <a href=\"{url}\">možnosti na Bookingu</a> na vaše dátumy",
        "transfer": "🚕 transfer tam a späť ~{x}€ (odhad)",
        "total": "💶 <b>Spolu {t}€</b>",
        "total_no_stay": "💶 <b>Letenky a transfer {t}€</b>, ubytovanie navyše",
        "total_diamond": "💶 <b>{t}€ za obe letenky</b>",
        "party": " pre {a} dosp.{k}",
        "party_kids": " + {n} deti",
        "new": " · nové", "cheaper": " · o {x}% lacnejšie ako zvyčajne",
        "weather_f": "🌤 predpoveď: do {t}°C, daždivých dní {r} z {n}",
        "weather_y": "🌡 v tieto dni pred rokom: do {t}°C, daždivých dní {r} z {n}",
        "entry": {"evisa": "🛂 treba elektronické vízum", "on_arrival": "🛂 vízum po prílete", "eta": "🛂 treba vopred elektronickú registráciu", "residence": "🛂 bez víza na pobyt v EÚ", "unknown": ""},
        "lost": "Stratené pracovné dni: {n}",
        "school": "vynechané školské dni: {n}",
        "slots": {"morning": "odlet skoro ráno", "evening_before": "odlet večer po práci", "midday": "odlet cez deň", "day_lost": "odlet neskoro, deň padne na cestu",
                  "before_work": "prílet ráno rovno do práce", "evening": "späť večer", "afternoon": "späť cez deň", "ret_day_lost": "späť ráno, posledný deň padne na cestu"},
        "btn_country": "👎 krajina", "btn_city": "👎 mesto", "btn_stay": "👎 ubytovanie", "btn_out": "👎 let tam", "btn_back": "👎 let späť", "btn_up": "👍 páči sa",
        "vac_option": "{a} - {b}, {who}", "vac_hotels": "Vaše hotely v tieto dni:", "vac_no_flights": "priame lety v tieto dni zatiaľ nie sú",
        "tickets": "letenky",
        "footer": "Sú to len odporúčania: rezervujete si sami, ceny sa môžu zmeniť. Nastavenia: {url}",
        "no_citizenship": "Zadajte v nastaveniach občianstvo, aby sme vynechali krajiny s vízami.",
        "days": ["po", "ut", "st", "št", "pi", "so", "ne"],
    },
    "en": {
        "title": "WE Weekends · {d}",
        "head": "Kiwi and hotel prices as of {d}. Total covers return flights for everyone, the stay for all nights and a transfer reserve (an estimate). Ryanair and Wizz Air often sell the same flights cheaper directly, compare before buying.",
        "weekends": "Weekends", "solo": "solo", "family": "family", "diamonds": "Diamonds: flights without a hotel",
        "vacation": "Holiday", "wow": "Last-minute packages", "from": "from",
        "nights": "nights: {n}",
        "flight": "🛫 <a href=\"{url}\">{air}</a> {dest}, out {o}, back {r} · {p}€{who}",
        "who_family": " for everyone",
        "stay": "🏨 <a href=\"{url}\">{name}</a>{place} ({rating}) · {p}€ for {n}",
        "stay_rating": "TripAdvisor {r} of 5, {c} reviews",
        "stay_nights": {1: "night", 2: "nights", 5: "nights"},
        "stay_none": "🏨 no stay price found: <a href=\"{url}\">options on Booking</a> for your dates",
        "transfer": "🚕 transfer both ways ~{x}€ (estimate)",
        "total": "💶 <b>Total {t}€</b>",
        "total_no_stay": "💶 <b>Flights and transfer {t}€</b>, stay extra",
        "total_diamond": "💶 <b>{t}€ for both flights</b>",
        "party": " for {a} adults{k}",
        "party_kids": " + {n} kids",
        "new": " · new", "cheaper": " · {x}% cheaper than usual",
        "weather_f": "🌤 forecast: up to {t}°C, rainy days {r} of {n}",
        "weather_y": "🌡 these days last year: up to {t}°C, rainy days {r} of {n}",
        "entry": {"evisa": "🛂 e-visa needed", "on_arrival": "🛂 visa on arrival", "eta": "🛂 travel authorisation needed in advance", "residence": "🛂 visa-free with an EU residence permit", "unknown": ""},
        "lost": "Workdays lost: {n}",
        "school": "school days missed: {n}",
        "slots": {"morning": "early morning departure", "evening_before": "leaves in the evening after work", "midday": "daytime departure", "day_lost": "late departure, the day goes on travel",
                  "before_work": "lands in the morning, straight to work", "evening": "back in the evening", "afternoon": "back in the afternoon", "ret_day_lost": "back in the morning, the last day goes on travel"},
        "btn_country": "👎 country", "btn_city": "👎 city", "btn_stay": "👎 stay", "btn_out": "👎 outbound", "btn_back": "👎 return flight", "btn_up": "👍 like",
        "vac_option": "{a} - {b}, {who}", "vac_hotels": "Your hotels on these dates:", "vac_no_flights": "no direct flights on these dates yet",
        "tickets": "tickets",
        "footer": "Recommendations only: you book yourself, prices may change. Settings: {url}",
        "no_citizenship": "Set your citizenship in the settings to skip countries that need a visa.",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    },
}


@lru_cache(maxsize=1)
def _countries() -> dict:
    return json.loads((DATA / "countries.json").read_text(encoding="utf-8"))


def country_name(code: str, lang: str) -> str:
    return _countries().get(lang, {}).get(code, code)


def place_name(pid: str, lang: str) -> str:
    place = GEO.get("places", {}).get(pid) or {}
    return place.get(lang) or place.get("en") or ""


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def flag(country: str) -> str:
    if len(country) != 2 or not country.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in country.upper())


def _plural(forms: dict, lang: str, n: int) -> str:
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return forms[1]
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return forms[2]
        return forms[5]
    if lang == "sk":
        return forms[1] if n == 1 else forms[2] if 2 <= n <= 4 else forms[5]
    return forms[1] if n == 1 else forms[2]


def plural(lang: str, n: int) -> str:
    return _plural(TX[lang]["stay_nights"], lang, n)


def day(lang: str, d: date) -> str:
    return f"{d.day}.{d.month:02d} ({TX[lang]['days'][d.weekday()]})"


def when(lang: str, d: datetime) -> str:
    return f"{TX[lang]['days'][d.weekday()]} {d:%d.%m} {d:%H:%M}"


def party(lang: str, adults: int, kids: int) -> str:
    t = TX[lang]
    return t["party"].format(a=adults, k=t["party_kids"].format(n=kids) if kids else "")


def item_text(lang: str, c: Candidate, city_name: str, adults: int = 1, kids: int = 0, new: bool = True) -> str:
    t = TX[lang]
    it = c.it
    icon = "💎" if c.mode == "diamond" else ("🌊" if "sea" in c.tags else "⛰" if "mountains" in c.tags else "🏙")
    lines = [f"{icon} <b>{e(city_name)}, {e(country_name(it.country, lang))}</b>  {e(day(lang, it.depart.date()))} - {e(day(lang, it.back.date()))}, {t['nights'].format(n=it.nights)}"]
    out = f"{it.depart:%H:%M}→{it.arrive:%H:%M}"
    back = f"{it.ret_depart:%H:%M}→{it.back:%H:%M}"
    url = it.url if it.url.startswith("https://") else "https://www.kiwi.com/"
    who = t["who_family"] if c.mode == "family" else ""
    lines.append(t["flight"].format(url=e(url), air=e(it.airline or t["tickets"]), dest=e(it.dest), o=e(out), r=e(back), p=round(c.flight_price), who=who))
    if c.mode != "diamond":
        if c.hotel_price is not None:
            rating = t["stay_rating"].format(r=f"{(c.hotel_rating or 0) / 2:.1f}", c=c.hotel_reviews or 0)
            place = f", {e(place_name(c.hotel_place, lang))}" if c.hotel_place else ""
            lines.append(t["stay"].format(url=e(c.hotel_url), name=e(c.hotel_name), place=place, rating=e(rating),
                                          p=round(c.hotel_price), n=f"{it.nights} {plural(lang, it.nights)}"))
        else:
            lines.append(t["stay_none"].format(url=e(c.hotel_url)))
        if c.transfer:
            lines.append(t["transfer"].format(x=round(c.transfer)))
    if c.weather:
        lines.append(t["weather_f" if c.weather.forecast else "weather_y"].format(t=round(c.weather.tmax), r=c.weather.rain_days, n=c.weather.days))
    if t["entry"].get(c.entry):
        lines.append(t["entry"][c.entry])
    who_total = party(lang, adults, kids) if c.mode == "family" else ""
    if c.mode == "diamond":
        total = t["total_diamond"].format(t=round(c.flight_price))
    elif c.hotel_price is not None:
        total = t["total"].format(t=round(c.total)) + who_total
    else:
        total = t["total_no_stay"].format(t=round(c.total))
    if c.usual and c.flight_price < c.usual * 0.9:
        total += t["cheaper"].format(x=round(100 * (1 - c.flight_price / c.usual)))
    elif new:
        total += t["new"]
    lines.append(total)
    facts = [t["lost"].format(n=c.workdays_lost), t["slots"][c.out_slot],
             t["slots"]["ret_day_lost" if c.ret_slot == "day_lost" else c.ret_slot]]
    if c.mode == "family" and c.school_days_lost:
        facts.append(t["school"].format(n=c.school_days_lost))
    lines.append("; ".join(facts))
    return "\n".join(lines)


def buttons(lang: str, ids: dict[str, str], mode: str) -> list[list[dict]]:
    """ids maps an aspect (country, city, stay, out, back) to its stored
    button id. Outbound and return flights are separate signals."""
    t = TX[lang]
    down = lambda key: {"text": t[f"btn_{key}"], "callback_data": f"r:{ids[key]}:-1"}  # noqa: E731
    row1 = [down("country"), down("city")]
    if mode != "diamond" and "stay" in ids:
        row1.append(down("stay"))
    return [row1, [down("out"), down("back")], [{"text": t["btn_up"], "callback_data": f"r:{ids['city']}:1"}]]

"""The daily message. Everything taken from outside (city and hotel names,
airlines, links) is escaped before it goes into Telegram HTML."""
from __future__ import annotations

import html
from datetime import date, datetime

from .score import Candidate

TX = {
    "ru": {
        "title": "WE Weekends · {d}",
        "weekends": "Выходные", "solo": "один", "family": "семья", "diamonds": "Бриллианты: билеты без отеля",
        "vacation": "Отпуск", "wow": "Горящие пакеты", "from": "от",
        "nights": {1: "ночь", 2: "ночи", 5: "ночей"},
        "flight": "🛫 {p}€ {who}, {air}: туда {o}, обратно {r}",
        "who_solo": "на одного", "who_family": "на всех",
        "stay_price": "🏨 от {p}€ за {n} {nw}{approx}, {name}{rating}",
        "stay_link": "🏨 жильё не включено, смотрите на Booking",
        "stay_open": "жильё",
        "total": "💶 всего ≈ {t}€{tr}",
        "total_no_stay": "💶 билеты {t}€{tr}, без жилья",
        "transfer": " (с трансфером ~{x}€)",
        "lost": " · рабочих дней в поездке: {n}",
        "school": " · учебных дней: {n}",
        "weather_f": "🌤 прогноз: до {t}°C, дождливых дней {r} из {n}",
        "weather_y": "🌤 в эти дни год назад: до {t}°C, дождливых дней {r} из {n}",
        "cheaper": "📉 на {x}% дешевле обычного",
        "entry": {"evisa": "🛂 нужна электронная виза", "on_arrival": "🛂 виза по прилёте", "eta": "🛂 нужна электронная регистрация заранее", "residence": "🛂 без визы по виду на жительство в ЕС", "unknown": ""},
        "tickets": "билеты",
        "dates": "{a} → {b}",
        "vac_option": "{a} → {b}, {who}",
        "vac_hotels": "Ваши отели на эти даты:",
        "vac_no_flights": "прямых рейсов на эти даты пока нет",
        "vac_suggest_off": "Подбор новых отелей включится, когда подключим цены Booking.",
        "footer": "Это только рекомендации: бронируете вы сами, цены могут измениться. Настройки: {url}",
        "no_citizenship": "Укажите гражданство в настройках, чтобы не видеть стран, куда нужна виза.",
        "like": "👍", "dislike": "👎",
        "days": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    },
    "sk": {
        "title": "WE Weekends · {d}",
        "weekends": "Víkendy", "solo": "sólo", "family": "rodina", "diamonds": "Diamanty: letenky bez hotela",
        "vacation": "Dovolenka", "wow": "Last minute balíky", "from": "od",
        "nights": {1: "noc", 2: "noci", 5: "nocí"},
        "flight": "🛫 {p}€ {who}, {air}: tam {o}, späť {r}",
        "who_solo": "pre jedného", "who_family": "pre všetkých",
        "stay_price": "🏨 od {p}€ za {n} {nw}{approx}, {name}{rating}",
        "stay_link": "🏨 ubytovanie nie je zahrnuté, pozrite Booking",
        "stay_open": "ubytovanie",
        "total": "💶 spolu ≈ {t}€{tr}",
        "total_no_stay": "💶 letenky {t}€{tr}, bez ubytovania",
        "transfer": " (s transferom ~{x}€)",
        "lost": " · pracovných dní na ceste: {n}",
        "school": " · školských dní: {n}",
        "weather_f": "🌤 predpoveď: do {t}°C, daždivých dní {r} z {n}",
        "weather_y": "🌤 v tieto dni pred rokom: do {t}°C, daždivých dní {r} z {n}",
        "cheaper": "📉 o {x}% lacnejšie ako zvyčajne",
        "entry": {"evisa": "🛂 treba elektronické vízum", "on_arrival": "🛂 vízum po prílete", "eta": "🛂 treba vopred elektronickú registráciu", "residence": "🛂 bez víza na pobyt v EÚ", "unknown": ""},
        "tickets": "letenky",
        "dates": "{a} → {b}",
        "vac_option": "{a} → {b}, {who}",
        "vac_hotels": "Vaše hotely v tieto dni:",
        "vac_no_flights": "priame lety v tieto dni zatiaľ nie sú",
        "vac_suggest_off": "Výber nových hotelov sa zapne po pripojení cien Bookingu.",
        "footer": "Sú to len odporúčania: rezervujete si sami, ceny sa môžu zmeniť. Nastavenia: {url}",
        "no_citizenship": "Zadajte v nastaveniach občianstvo, aby sme vynechali krajiny s vízami.",
        "like": "👍", "dislike": "👎",
        "days": ["po", "ut", "st", "št", "pi", "so", "ne"],
    },
    "en": {
        "title": "WE Weekends · {d}",
        "weekends": "Weekends", "solo": "solo", "family": "family", "diamonds": "Diamonds: flights without a hotel",
        "vacation": "Holiday", "wow": "Last-minute packages", "from": "from",
        "nights": {1: "night", 2: "nights", 5: "nights"},
        "flight": "🛫 {p}€ {who}, {air}: out {o}, back {r}",
        "who_solo": "for one", "who_family": "for everyone",
        "stay_price": "🏨 from {p}€ for {n} {nw}{approx}, {name}{rating}",
        "stay_link": "🏨 stay not included, see Booking",
        "stay_open": "stay",
        "total": "💶 total ≈ {t}€{tr}",
        "total_no_stay": "💶 flights {t}€{tr}, no stay",
        "transfer": " (incl. transfer ~{x}€)",
        "lost": " · workdays on the trip: {n}",
        "school": " · school days: {n}",
        "weather_f": "🌤 forecast: up to {t}°C, rainy days {r} of {n}",
        "weather_y": "🌤 these days last year: up to {t}°C, rainy days {r} of {n}",
        "cheaper": "📉 {x}% cheaper than usual",
        "entry": {"evisa": "🛂 e-visa needed", "on_arrival": "🛂 visa on arrival", "eta": "🛂 travel authorisation needed in advance", "residence": "🛂 visa-free with an EU residence permit", "unknown": ""},
        "tickets": "tickets",
        "dates": "{a} → {b}",
        "vac_option": "{a} → {b}, {who}",
        "vac_hotels": "Your hotels on these dates:",
        "vac_no_flights": "no direct flights on these dates yet",
        "vac_suggest_off": "New hotel suggestions start once Booking prices are connected.",
        "footer": "Recommendations only: you book yourself, prices may change. Settings: {url}",
        "no_citizenship": "Set your citizenship in the settings to skip countries that need a visa.",
        "like": "👍", "dislike": "👎",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    },
}


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def flag(country: str) -> str:
    if len(country) != 2 or not country.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in country.upper())


def plural(lang: str, n: int) -> str:
    forms = TX[lang]["nights"]
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return forms[1]
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return forms[2]
        return forms[5]
    if lang == "sk":
        return forms[1] if n == 1 else forms[2] if 2 <= n <= 4 else forms[5]
    return forms[1] if n == 1 else forms[2]


def when(lang: str, d: datetime) -> str:
    return f"{TX[lang]['days'][d.weekday()]} {d:%d.%m} {d:%H:%M}"


def day(lang: str, d: date) -> str:
    return f"{TX[lang]['days'][d.weekday()]} {d:%d.%m}"


def item_text(lang: str, c: Candidate, city_name: str) -> str:
    t = TX[lang]
    it = c.it
    lines = [f"{flag(it.country)} <b>{e(city_name)}</b> ({e(it.dest)}) · {e(day(lang, it.depart.date()))} → {e(day(lang, it.back.date()))}, {it.nights} {plural(lang, it.nights)}"]
    who = t["who_family"] if c.mode == "family" else t["who_solo"]
    lines.append(t["flight"].format(p=round(c.flight_price), who=who, air=e(it.airline), o=e(when(lang, it.depart)), r=e(when(lang, it.ret_depart))))
    if c.mode != "diamond":
        if c.hotel_price is not None:
            rating = f" · {c.hotel_rating}" if c.hotel_rating else ""
            approx = " ≈" if c.hotel_approx else ""
            lines.append(t["stay_price"].format(p=round(c.hotel_price), n=it.nights, nw=plural(lang, it.nights), approx=approx,
                                                name=f'<a href="{e(c.hotel_url)}">{e(c.hotel_name or t["stay_open"])}</a>', rating=e(rating)))
        else:
            lines.append(f'<a href="{e(c.hotel_url)}">{t["stay_link"]}</a>')
    tr = t["transfer"].format(x=round(c.transfer)) if c.transfer else ""
    extra = t["lost"].format(n=c.workdays_lost)
    if c.mode == "family" and c.school_days_lost:
        extra += t["school"].format(n=c.school_days_lost)
    if c.mode != "diamond":
        key = "total" if c.hotel_price is not None else "total_no_stay"
        lines.append(t[key].format(t=round(c.total), tr=tr) + extra)
    else:
        lines[-1] += extra
    if c.weather:
        lines.append(t["weather_f" if c.weather.forecast else "weather_y"].format(t=round(c.weather.tmax), r=c.weather.rain_days, n=c.weather.days))
    if c.usual and c.flight_price < c.usual * 0.9:
        lines.append(t["cheaper"].format(x=round(100 * (1 - c.flight_price / c.usual))))
    if t["entry"].get(c.entry):
        lines.append(t["entry"][c.entry])
    if it.url.startswith("https://"):
        lines.append(f'<a href="{e(it.url)}">{t["tickets"]} →</a>')
    return "\n".join(lines)


def pack(blocks: list[str], limit: int = 3800) -> list[str]:
    """Splits the digest into Telegram-sized messages on block boundaries."""
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages

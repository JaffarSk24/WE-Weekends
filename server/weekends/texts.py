"""Bot texts in Russian, Slovak and English. Placeholders are filled with
values the bot controls; nothing typed by a user is ever inserted here."""
from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "welcome": (
            "<b>WE Weekends</b> присылает персональную подборку дешёвых поездок: выходные у моря и в горах, "
            "очень дешёвые билеты, горящие пакеты и цены на отели для отпуска.\n\n"
            "Настройте, что и как часто присылать: кнопка «Открыть настройки» ниже или «Настройки» в меню. Это же можно сделать в браузере: https://jaffarsk24.github.io/WE-Weekends/\n\n"
            "Это только рекомендации: бронируете вы сами у авиакомпаний и отелей. Мы не турагентство, ничего не продаём "
            "и не получаем комиссий от партнёров. Сервис бесплатный."
        ),
        "open_site": "Открыть настройки",
        "menu": "Настройки",
        "login_code": (
            "Код для входа на сайт WE Weekends: <code>{code}</code>\n\n"
            "Введите его на странице, с которой вы начали вход. Код действует 10 минут. "
            "Никому его не сообщайте. Если вход начинали не вы, просто ничего не делайте."
        ),
        "login_bad": "Ссылка для входа устарела или уже использована. Начните вход на сайте заново.",
        "slow_down": "Слишком много запросов. Попробуйте через несколько минут.",
        "paused": "Рассылка на паузе. Вернуть: /resume",
        "resumed": "Рассылка снова включена.",
        "no_account": "Сначала войдите на сайт и сохраните настройки.",
        "logout_all": "Вы вышли на всех устройствах ({n}).",
        "delete_ask": "Удалить аккаунт и все настройки без возможности восстановления?",
        "delete_yes": "Да, удалить",
        "delete_no": "Нет",
        "deleted": "Аккаунт и настройки удалены. Чтобы вернуться, просто войдите на сайт снова.",
        "kept": "Ничего не удалено.",
        "help": (
            "/settings: открыть настройки\n/pause: поставить рассылку на паузу\n/resume: включить рассылку\n"
            "/logout_all: выйти на всех устройствах\n/delete: удалить аккаунт\n/language: язык"
        ),
        "promoted": "Место освободилось: ваш аккаунт WE Weekends активен, рассылка начнётся по вашим настройкам.",
        "language": "Выберите язык:",
        "language_set": "Язык: русский.",
        "rated": "Учтено: такие варианты будут выше или ниже в подборке.",
        "unrated": "Отметка снята.",
    },
    "sk": {
        "welcome": (
            "<b>WE Weekends</b> posiela osobný výber lacných ciest: víkendy pri mori a na horách, "
            "veľmi lacné letenky, last minute balíky a ceny hotelov na dovolenku.\n\n"
            "Nastavte, čo a ako často posielať: tlačidlo „Otvoriť nastavenia“ nižšie alebo „Nastavenia“ v menu. To isté v prehliadači: https://jaffarsk24.github.io/WE-Weekends/\n\n"
            "Sú to len odporúčania: rezervujete si sami u leteckých spoločností a hotelov. Nie sme cestovná kancelária, nič "
            "nepredávame a od partnerov nedostávame provízie. Služba je zadarmo."
        ),
        "open_site": "Otvoriť nastavenia",
        "menu": "Nastavenia",
        "login_code": (
            "Kód na prihlásenie do WE Weekends: <code>{code}</code>\n\n"
            "Zadajte ho na stránke, kde ste začali prihlásenie. Kód platí 10 minút. "
            "Nikomu ho nehovorte. Ak ste prihlásenie nezačali vy, nerobte nič."
        ),
        "login_bad": "Odkaz na prihlásenie vypršal alebo už bol použitý. Začnite prihlásenie na stránke znova.",
        "slow_down": "Príliš veľa požiadaviek. Skúste to o pár minút.",
        "paused": "Posielanie je pozastavené. Obnoviť: /resume",
        "resumed": "Posielanie je opäť zapnuté.",
        "no_account": "Najprv sa prihláste na stránke a uložte nastavenia.",
        "logout_all": "Odhlásili ste sa na všetkých zariadeniach ({n}).",
        "delete_ask": "Natrvalo vymazať účet a všetky nastavenia?",
        "delete_yes": "Áno, vymazať",
        "delete_no": "Nie",
        "deleted": "Účet a nastavenia sú vymazané. Ak sa chcete vrátiť, stačí sa znova prihlásiť na stránke.",
        "kept": "Nič sa nevymazalo.",
        "help": (
            "/settings: otvoriť nastavenia\n/pause: pozastaviť posielanie\n/resume: zapnúť posielanie\n"
            "/logout_all: odhlásiť všade\n/delete: vymazať účet\n/language: jazyk"
        ),
        "promoted": "Uvoľnilo sa miesto: váš účet WE Weekends je aktívny, posielanie začne podľa vašich nastavení.",
        "language": "Vyberte jazyk:",
        "language_set": "Jazyk: slovenčina.",
        "rated": "Zaznamenané: podobné možnosti budú vo výbere vyššie alebo nižšie.",
        "unrated": "Označenie zrušené.",
    },
    "en": {
        "welcome": (
            "<b>WE Weekends</b> sends a personal selection of cheap trips: weekends by the sea and in the mountains, "
            "very cheap flights, last-minute packages and hotel prices for your holiday.\n\n"
            "Choose what to receive and how often: the \"Open settings\" button below or \"Settings\" in the menu. Or in a browser: https://jaffarsk24.github.io/WE-Weekends/\n\n"
            "These are only recommendations: you book directly with airlines and hotels yourself. We are not a travel "
            "agency, we sell nothing and take no commissions from partners. The service is free."
        ),
        "open_site": "Open settings",
        "menu": "Settings",
        "login_code": (
            "Your WE Weekends sign-in code: <code>{code}</code>\n\n"
            "Enter it on the page where you started signing in. The code is valid for 10 minutes. "
            "Never share it. If you did not start signing in, just ignore this."
        ),
        "login_bad": "This sign-in link has expired or was already used. Start signing in on the website again.",
        "slow_down": "Too many requests. Please try again in a few minutes.",
        "paused": "Messages are paused. Resume: /resume",
        "resumed": "Messages are on again.",
        "no_account": "Sign in on the website and save your settings first.",
        "logout_all": "You are signed out on all devices ({n}).",
        "delete_ask": "Delete your account and all settings permanently?",
        "delete_yes": "Yes, delete",
        "delete_no": "No",
        "deleted": "Your account and settings are deleted. To come back, just sign in on the website again.",
        "kept": "Nothing was deleted.",
        "help": (
            "/settings: open settings\n/pause: pause messages\n/resume: resume messages\n"
            "/logout_all: sign out everywhere\n/delete: delete account\n/language: language"
        ),
        "promoted": "A place opened up: your WE Weekends account is active and messages will start according to your settings.",
        "language": "Choose a language:",
        "language_set": "Language: English.",
        "rated": "Noted: options like this will rank higher or lower.",
        "unrated": "Mark removed.",
    },
}

COMMANDS = {
    "ru": [("settings", "Настройки"), ("pause", "Пауза"), ("resume", "Включить рассылку"), ("logout_all", "Выйти везде"), ("delete", "Удалить аккаунт"), ("language", "Язык"), ("help", "Помощь")],
    "sk": [("settings", "Nastavenia"), ("pause", "Pozastaviť"), ("resume", "Zapnúť posielanie"), ("logout_all", "Odhlásiť všade"), ("delete", "Vymazať účet"), ("language", "Jazyk"), ("help", "Pomoc")],
    "en": [("settings", "Settings"), ("pause", "Pause"), ("resume", "Resume messages"), ("logout_all", "Sign out everywhere"), ("delete", "Delete account"), ("language", "Language"), ("help", "Help")],
}


def lang_of(code: str | None) -> str:
    code = (code or "").lower()
    if code.startswith(("ru", "uk", "be", "kk")):
        return "ru"
    if code.startswith(("sk", "cs")):
        return "sk"
    return "en"


def t(lang: str, key: str, **values: object) -> str:
    text = TEXTS.get(lang, TEXTS["en"])[key]
    return text.format(**values) if values else text

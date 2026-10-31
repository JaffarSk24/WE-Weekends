"""Last-minute package deals from travel agencies' newsletters.

The newsletters arrive in an Atom feed (WOW_FEED_URL, kept out of the
repository). Each one is reduced to its headline and the lowest "from X €"
price. Links inside the e-mails carry the subscriber's tracking, so they are
never used: the item points to the agency's public last-minute page."""
from __future__ import annotations

import html
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import defusedxml.ElementTree as ET
import httpx

log = logging.getLogger("weekends.wow")
ATOM = {"a": "http://www.w3.org/2005/Atom"}

AGENCIES = {
    "invia": ("Invia", "https://www.invia.sk/dovolenka/last-minute/"),
    "satur": ("Satur", "https://www.satur.sk/"),
    "pelikan": ("Pelikán", "https://www.pelikan.sk/sk/last-minute/"),
    "tatratour": ("Tatratour", "https://tatratour.sk/info/lastminute/"),
}
PRICE = re.compile(r"(?:od|from|за|от)?\s*(\d{1,2}(?:[ \u00a0]\d{3})|\d{2,5})\s?€", re.I)


@dataclass(frozen=True)
class Deal:
    id: str
    title: str
    price: int | None
    agency: str
    url: str
    at: datetime


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(fragment))).strip()


class WowFeed:
    def __init__(self) -> None:
        self.url = os.environ.get("WOW_FEED_URL", "").strip()
        self.client = httpx.Client(timeout=30.0, follow_redirects=False)
        self._cache: tuple[float, list[Deal]] = (0.0, [])

    def deals(self, days: int = 4) -> list[Deal]:
        if not self.url.startswith("https://"):
            return []
        if time.time() - self._cache[0] < 3 * 3600:
            return self._cache[1]
        try:
            response = self.client.get(self.url)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                return []
            root = ET.fromstring(response.content)
        except Exception as exc:  # the feed is optional; never break the digest
            log.warning("wow feed failed: %s", type(exc).__name__)
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        for entry in root.findall("a:entry", ATOM):
            try:
                at = datetime.fromisoformat((entry.findtext("a:updated", default="", namespaces=ATOM) or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if at < cutoff:
                continue
            title = _text(entry.findtext("a:title", default="", namespaces=ATOM))[:140]
            body = _text(entry.findtext("a:content", default="", namespaces=ATOM))
            agency = next((AGENCIES[k] for k in AGENCIES if k in body.lower()), None)
            if not title or agency is None:
                continue
            prices = [int(p.replace(" ", "").replace("\u00a0", "")) for p in PRICE.findall(title + " " + body[:4000])]
            prices = [p for p in prices if 50 <= p <= 20000]
            out.append(Deal(id=entry.findtext("a:id", default=title, namespaces=ATOM)[:200], title=title,
                            price=min(prices) if prices else None, agency=agency[0], url=agency[1], at=at))
        self._cache = (time.time(), out)
        return out

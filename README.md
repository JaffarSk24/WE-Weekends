# WE Weekends

A free personal Telegram feed of cheap trips. You set departure airports, countries, budgets for solo and family trips, workdays, public and school holidays, and how often to hear from us. Every day the service looks for direct flights and places to stay, scores each option by your rules and sends only what passes.

Settings page: https://jaffarsk24.github.io/WE-Weekends/ · Bot: [@WEWeekendsBot](https://t.me/WEWeekendsBot)

## Recommendations only

WE Weekends sells and books nothing. You book directly with airlines and hotels yourself. We are not a travel agency and take no commissions from partners. Prices come from public sources and may change before you book.

## How it is built

- `web/`: the settings page, plain HTML, CSS and JavaScript modules with no dependencies, published on GitHub Pages.
- `server/`: the gateway (Python, FastAPI, SQLite) that stores settings, signs users in through the Telegram bot and runs the search. It runs in Docker behind nginx.

The search is plain deterministic code, so the service costs the same no matter how many people use it: searches are shared per departure airport and filtered for each user.

## Status

- Sign-in through Telegram, settings storage, bot commands: done.
- Search engine and the daily digest: in progress.

## Development

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.txt && pip install -r requirements-dev.txt
pytest -q
```

Run the page locally with `python3 -m http.server 8791` in `web/`; it talks to a gateway on `localhost:8790`.

Security: see [SECURITY.md](SECURITY.md). License: MIT.

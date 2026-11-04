import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weekends.app import create_app  # noqa: E402
from weekends.config import Config  # noqa: E402

SECRET = "s" * 64
OWNER = 1000


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.calls = []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


class NoStayforlong:
    """Stands in for Stayforlong so tests never reach the network."""

    def __init__(self, *a, **kw):
        self.calls = 0

    def resolve(self, *a, **kw):
        return None

    def best(self, *a, **kw):
        return None


@pytest.fixture(autouse=True)
def no_stayforlong(monkeypatch):
    from weekends.engine import run
    monkeypatch.setattr(run, "Stayforlong", NoStayforlong)


@pytest.fixture
def env(tmp_path):
    cfg = Config(
        bot_token="123456:" + "A" * 35,
        bot_username="WEWeekendsBot",
        webhook_secret=SECRET,
        code_pepper=b"p" * 32,
        owner_chat_id=OWNER,
        db_path=str(tmp_path / "t.sqlite"),
        web_origins=("https://jaffarsk24.github.io",),
        web_url="https://jaffarsk24.github.io/WE-Weekends/",
        max_active_users=2,
        trust_proxy=True,
        telegram_ipv4=True,
    )
    tg = FakeTelegram()
    app = create_app(cfg, tg)
    client = TestClient(app)
    return client, tg, app


def run_update(app, update):
    # The webhook hands updates to a thread; tests call the handler directly.
    app.state.bot.handle(update)


def message(chat_id, text, lang="ru"):
    return {"message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "is_bot": False, "language_code": lang}, "text": text}}


def login(client, app, tg, chat_id, ip="1.1.1.1"):
    started = client.post("/v1/auth/start", headers={"x-real-ip": ip}).json()
    run_update(app, message(chat_id, f"/start l_{started['nonce']}"))
    code = tg.sent[-1][1].split("<code>")[1].split("</code>")[0]
    res = client.post("/v1/auth/verify", json={"nonce": started["nonce"], "secret": started["secret"], "code": code},
                      headers={"x-real-ip": ip})
    assert res.status_code == 200, res.text
    return res.json()["token"]

from conftest import OWNER, SECRET, login, message, run_update


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_flow_and_defaults(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["status"] == "active"
    assert me["configured"] is False
    assert me["settings"]["profile"]["origins"] == ["BTS"]
    assert me["lang"] == "ru"


def test_code_without_secret_is_useless(env):
    client, tg, app = env
    started = client.post("/v1/auth/start").json()
    run_update(app, message(7, f"/start l_{started['nonce']}"))
    code = tg.sent[-1][1].split("<code>")[1].split("</code>")[0]
    wrong = "A" * 43
    res = client.post("/v1/auth/verify", json={"nonce": started["nonce"], "secret": wrong, "code": code})
    assert res.status_code == 400


def test_code_attempts_are_limited(env):
    client, tg, app = env
    started = client.post("/v1/auth/start").json()
    run_update(app, message(7, f"/start l_{started['nonce']}"))
    code = tg.sent[-1][1].split("<code>")[1].split("</code>")[0]
    bad = f"{(int(code) + 1) % 1_000_000:06d}"
    body = {"nonce": started["nonce"], "secret": started["secret"]}
    for _ in range(5):
        assert client.post("/v1/auth/verify", json={**body, "code": bad}).status_code == 400
    # Even the right code fails once the attempts are spent.
    assert client.post("/v1/auth/verify", json={**body, "code": code}).status_code == 400


def test_nonce_cannot_be_hijacked_by_second_chat(env):
    client, tg, app = env
    started = client.post("/v1/auth/start").json()
    run_update(app, message(7, f"/start l_{started['nonce']}"))
    run_update(app, message(8, f"/start l_{started['nonce']}"))
    assert "<code>" not in tg.sent[-1][1]


def test_pending_login_reports_pending(env):
    client, _, _ = env
    started = client.post("/v1/auth/start").json()
    res = client.post("/v1/auth/verify", json={"nonce": started["nonce"], "secret": started["secret"], "code": "000000"})
    assert res.status_code == 409


def test_settings_roundtrip_and_conflict(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    me = client.get("/v1/me", headers=auth(token)).json()
    settings = me["settings"]
    settings["profile"]["origins"] = ["BTS", "VIE"]
    settings["blocks"]["vacation"]["hotels"] = ["https://www.booking.com/hotel/tr/lonicera-resort-spa.ru.html?aid=1&x=2"]
    res = client.put("/v1/settings", json={"version": 0, "lang": "en", "settings": settings}, headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["version"] == 1
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["settings"]["blocks"]["vacation"]["hotels"] == ["https://www.booking.com/hotel/tr/lonicera-resort-spa.html"]
    assert me["lang"] == "en"
    stale = client.put("/v1/settings", json={"version": 0, "lang": "en", "settings": settings}, headers=auth(token))
    assert stale.status_code == 409


def test_settings_reject_bad_input(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    settings = client.get("/v1/me", headers=auth(token)).json()["settings"]
    cases = [
        ("profile", "origins", ["XXX"]),
        ("profile", "hacked", True),
        ("budgets", "solo", {"enabled": True, "budget_eur": -5}),
        ("blocks", "vacation", {**settings["blocks"]["vacation"], "hotels": ["https://evil.example/hotel/tr/x.html"]}),
        ("blocks", "vacation", {**settings["blocks"]["vacation"], "hotels": ["javascript:alert(1)"]}),
        ("geography", "include", ["<script>"]),
    ]
    for section, key, value in cases:
        bad = {**settings, section: {**settings[section], key: value}}
        res = client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": bad}, headers=auth(token))
        assert res.status_code == 422, (section, key)
        assert "<script>" not in res.text and "evil" not in res.text


def test_preset_overrides_weights(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    settings = client.get("/v1/me", headers=auth(token)).json()["settings"]
    settings["rating"] = {"preset": "comfort", "weights": {**settings["rating"]["weights"], "price": 0}}
    client.put("/v1/settings", json={"version": 0, "lang": "ru", "settings": settings}, headers=auth(token))
    saved = client.get("/v1/me", headers=auth(token)).json()["settings"]
    assert saved["rating"]["weights"]["price"] == 5


def test_auth_required_and_token_garbage(env):
    client, _, _ = env
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=auth("x" * 43)).status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_body_limit(env):
    client, tg, app = env
    token = login(client, app, tg, 42)
    res = client.put("/v1/settings", content=b"{" + b" " * 70000 + b"}",
                     headers={**auth(token), "content-type": "application/json"})
    assert res.status_code == 413


def test_auth_start_rate_limited(env):
    client, _, _ = env
    codes = [client.post("/v1/auth/start", headers={"x-real-ip": "9.9.9.9"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


def test_cors_only_for_pages_origin(env):
    client, _, _ = env
    ok = client.options("/v1/me", headers={"Origin": "https://jaffarsk24.github.io", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "https://jaffarsk24.github.io"
    evil = client.options("/v1/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers


def test_security_headers_and_no_docs(env):
    client, _, _ = env
    res = client.get("/v1/health")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_webhook_requires_secret(env):
    client, tg, _ = env
    upd = message(5, "/start")
    assert client.post("/tg", json=upd).status_code == 404
    assert client.post("/tg", json=upd, headers={"x-telegram-bot-api-secret-token": "wrong"}).status_code == 404
    assert client.post("/tg", json=upd, headers={"x-telegram-bot-api-secret-token": SECRET}).status_code == 200


def test_waitlist_and_promotion(env):
    client, tg, app = env
    t1 = login(client, app, tg, 1)
    login(client, app, tg, 2)
    t3 = login(client, app, tg, 3, ip="2.2.2.2")
    me3 = client.get("/v1/me", headers=auth(t3)).json()
    assert me3["status"] == "waitlist" and me3["waitlist_position"] == 1
    assert client.request("DELETE", "/v1/me", json={"confirm": "DELETE"}, headers=auth(t1)).status_code == 200
    assert client.get("/v1/me", headers=auth(t1)).status_code == 401
    assert client.get("/v1/me", headers=auth(t3)).json()["status"] == "active"
    assert any(chat == 3 for chat, _, _ in tg.sent)


def test_bot_ignores_groups_and_handles_commands(env):
    client, tg, app = env
    before = len(tg.sent)
    run_update(app, {"message": {"chat": {"id": -5, "type": "group"}, "from": {"id": 5}, "text": "/start"}})
    assert len(tg.sent) == before
    token = login(client, app, tg, 42)
    run_update(app, message(42, "/pause"))
    assert client.get("/v1/me", headers=auth(token)).json()["paused"] is True
    run_update(app, message(42, "/logout_all"))
    assert client.get("/v1/me", headers=auth(token)).status_code == 401


def test_stats_only_for_owner(env):
    _, tg, app = env
    run_update(app, message(55, "/stats"))
    assert "active:" not in tg.sent[-1][1]
    run_update(app, message(OWNER, "/stats"))
    assert "active:" in tg.sent[-1][1]

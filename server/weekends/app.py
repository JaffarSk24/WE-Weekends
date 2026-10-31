"""HTTP API for the settings page and the Telegram webhook.

Defence in depth, outermost first: nginx (TLS, body size, per-IP limits,
webhook path open only to Telegram's networks, fail2ban on floods), then
here: strict CORS, body size cap, per-IP / per-account rate limits, schema
validation that rejects unknown fields, bearer tokens checked in constant
time against hashes, and error bodies that never echo input.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.cors import CORSMiddleware

from . import config as config_module
from .auth import Auth, AuthError, Session
from .bot import Bot
from .db import Database
from .models import (
    DeleteRequest,
    PauseRequest,
    SaveRequest,
    TelegramAuthRequest,
    VerifyRequest,
)
from .ratelimit import RateLimiter
from .telegram import Telegram
from .users import Users, VersionConflict

log = logging.getLogger("weekends.app")

MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


def error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def create_app(cfg: config_module.Config | None = None, telegram: Telegram | None = None) -> FastAPI:
    cfg = cfg or config_module.load()
    db = Database(cfg.db_path)
    limiter = RateLimiter()
    auth = Auth(db, cfg.code_pepper, cfg.max_active_users)
    users = Users(db, cfg.max_active_users)
    tg = telegram or Telegram(cfg.bot_token, cfg.telegram_ipv4)
    bot = Bot(cfg, tg, auth, users, limiter)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.bot = bot

    def client_ip(request: Request) -> str:
        if cfg.trust_proxy:
            real = request.headers.get("x-real-ip", "")
            if real and len(real) <= 45:
                return real
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            response: Response = error(413, "too_large")
        elif request.headers.get("transfer-encoding"):
            response = error(411, "length_required")
        elif not limiter.allow("ip", client_ip(request)):
            response = error(429, "rate_limited")
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.web_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths only, never the submitted values.
        fields = sorted({".".join(str(p) for p in e.get("loc", ()) if p != "body") for e in exc.errors()})[:20]
        return JSONResponse({"error": "invalid", "fields": fields}, status_code=422)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return error(500, "server_error")

    def require_session(request: Request) -> Session | JSONResponse:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return error(401, "unauthorized")
        session = auth.session(header[7:].strip())
        if session is None:
            return error(401, "unauthorized")
        if not limiter.allow("account", str(session.chat_id)):
            return error(429, "rate_limited")
        return session

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/v1/auth/start")
    def auth_start(request: Request):
        if not limiter.allow("auth_start", client_ip(request)):
            return error(429, "rate_limited")
        started = auth.start()
        return {
            "nonce": started.nonce,
            "secret": started.secret,
            "link": f"https://t.me/{cfg.bot_username}?start=l_{started.nonce}",
            "expires_in": started.expires_in,
        }

    @app.post("/v1/auth/verify")
    def auth_verify(request: Request, body: VerifyRequest):
        if not limiter.allow("auth_verify", client_ip(request)):
            return error(429, "rate_limited")
        try:
            token, _ = auth.verify(body.nonce, body.secret, body.code)
        except AuthError as exc:
            # Only fixed codes ever leave the server, never exception text.
            code = {"expired": "expired", "pending": "pending"}.get(str(exc), "invalid")
            return error(409 if code == "pending" else 400, code)
        return {"token": token}

    @app.post("/v1/auth/telegram")
    def auth_telegram(request: Request, body: TelegramAuthRequest):
        if not limiter.allow("auth_verify", client_ip(request)):
            return error(429, "rate_limited")
        try:
            token, _ = auth.telegram_webapp(body.init_data, cfg.bot_token)
        except AuthError as exc:
            code = {"expired": "expired"}.get(str(exc), "invalid")
            return error(400, code)
        return {"token": token}

    @app.get("/v1/me")
    def me(request: Request):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        user = users.get(session.chat_id)
        if user is None:
            return error(401, "unauthorized")
        return user

    @app.put("/v1/settings")
    def save(request: Request, body: SaveRequest):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        if not limiter.allow("save", str(session.chat_id)):
            return error(429, "rate_limited")
        try:
            version = users.save(session.chat_id, body.version, body.lang, body.settings)
        except VersionConflict:
            return error(409, "version_conflict")
        return {"version": version}

    @app.post("/v1/pause")
    def pause(request: Request, body: PauseRequest):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        users.set_paused(session.chat_id, body.paused)
        return {"paused": body.paused}

    @app.post("/v1/logout")
    def logout(request: Request):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        auth.logout(session)
        return {"ok": True}

    @app.post("/v1/logout-all")
    def logout_all(request: Request):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        return {"sessions": auth.logout_all(session.chat_id)}

    @app.delete("/v1/me")
    def delete_me(request: Request, body: DeleteRequest):
        session = require_session(request)
        if isinstance(session, JSONResponse):
            return session
        auth.logout_all(session.chat_id)
        users.delete(session.chat_id)
        for chat_id, lang in users.promote_waitlist():
            bot.tg.send(chat_id, bot_promoted_text(lang), bot._site_button(lang))
        return {"ok": True}

    @app.post("/tg")
    async def webhook(request: Request):
        given = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(given.encode(), cfg.webhook_secret.encode()):
            return error(404, "not_found")
        try:
            update: Any = json.loads(await request.body())
        except ValueError:
            return {"ok": True}
        # Answer Telegram at once; the reply goes out from a worker thread.
        threading.Thread(target=_safe_handle, args=(bot, update), daemon=True).start()
        return {"ok": True}

    def housekeeping() -> None:
        while True:
            time.sleep(600)
            try:
                db.cleanup()
                for chat_id, lang in users.promote_waitlist():
                    bot.tg.send(chat_id, bot_promoted_text(lang), bot._site_button(lang))
            except Exception:
                log.exception("housekeeping failed")

    threading.Thread(target=housekeeping, daemon=True).start()
    return app


def bot_promoted_text(lang: str) -> str:
    from .texts import t

    return t(lang, "promoted")


def _safe_handle(bot: Bot, update: Any) -> None:
    try:
        bot.handle(update)
    except Exception:
        log.exception("update handling failed")

"""Web dashboard. Runs inside the bot process (same event loop) so it can use the live Discord client.

Stack: FastAPI, Jinja2 templates, htmx, Tabler (CSS), Authlib for the Discord OAuth2 login.
`ctx` is a namespace built in bot.py; it carries the bot, the database and the bot's own action functions.
"""
import contextlib
import logging
import secrets
from datetime import datetime
from pathlib import Path

import uvicorn
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .core import NAV, Denied, LoginRequired
from .modviews import router as mod_router
from .views import router

log = logging.getLogger("refract.dashboard")
HERE = Path(__file__).parent


def fmt_dt(value):
    """'2026-11-02T14:03:11+00:00' -> '2026-11-02 14:03 UTC'."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def create_app(ctx):
    app = FastAPI(title="Bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.tasks = set()  # keeps fire-and-forget tasks alive
    app.state.templates = templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["dt"] = fmt_dt
    templates.env.globals["nav"] = NAV
    app.add_middleware(SessionMiddleware, secret_key=ctx.session_secret, session_cookie="dash_session", max_age=7 * 86400,
                       same_site="lax", https_only=ctx.base_url.startswith("https"))

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "private, no-store")
        return resp

    @app.exception_handler(LoginRequired)
    async def login_required(request, exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Denied)
    async def denied(request, exc):
        return templates.TemplateResponse(request, "error.html", {"s": None, "message": exc.message, "flash": None, "path": ""},
                                          status_code=exc.status)

    oauth = OAuth()

    def discord_client():
        if "discord" not in oauth._clients:
            oauth.register("discord", client_id=ctx.bot.application_id, client_secret=ctx.client_secret,
                           authorize_url="https://discord.com/oauth2/authorize",
                           access_token_url="https://discord.com/api/oauth2/token", api_base_url="https://discord.com/api/v10/",
                           client_kwargs={"scope": "identify", "token_endpoint_auth_method": "client_secret_post"})
        return oauth.discord

    @app.get("/login")
    async def login(request: Request):
        return templates.TemplateResponse(request, "login.html", {"s": None, "flash": None, "path": ""})

    @app.get("/auth/discord")
    async def auth_discord(request: Request):
        return await discord_client().authorize_redirect(request, f"{ctx.base_url}/auth/callback")

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            token = await discord_client().authorize_access_token(request)
            me = (await discord_client().get("users/@me", token=token)).json()
            uid = int(me["id"])
        except (OAuthError, KeyError, ValueError):
            log.exception("dashboard login failed")
            raise Denied(400, "Discord login failed. Go back to the login page and try again.")
        nxt = request.session.get("next") or "/"
        request.session.clear()
        request.session.update(uid=uid, csrf=secrets.token_urlsafe(24))
        return RedirectResponse(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/", status_code=303)

    app.include_router(router)
    app.include_router(mod_router)
    return app


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield  # the bot owns SIGINT/SIGTERM


async def serve(ctx):
    config = uvicorn.Config(create_app(ctx), host=ctx.host, port=ctx.port, log_level="warning",
                            proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    log.info("dashboard on %s:%s (public %s)", ctx.host, ctx.port, ctx.base_url)
    await _Server(config).serve()

"""Standalone login service for login.heylark.dev (issue: centralized SSO login).

A minimal FastAPI app — two public routes, no pipeline/store dependencies:
  GET /              – render the Telegram Login Widget page; accepts ?next=<url>
  GET /auth/callback – validate widget payload, issue SSO cookie, redirect

Imports ONLY from job_hunter.tg_auth (auth primitives) and job_hunter.config;
no auth logic is duplicated here.

?next validation: only https://*.heylark.dev and https://heylark.dev are
accepted. Any other value (http://, foreign domain, proto-relative, relative
path) falls back to https://heylark.dev.  See is_safe_next().
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import tg_auth
from .config import Config, load_config

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

_ALLOWED_HOST_SUFFIX = ".heylark.dev"
_ALLOWED_BARE_HOST = "heylark.dev"
DEFAULT_POST_LOGIN = "https://heylark.dev"


# --- Open-redirect guard ----------------------------------------------------


def is_safe_next(url: str) -> bool:
    """Return True iff ``url`` is safe to redirect to after login.

    Allowed: https://heylark.dev  and  https://<anything>.heylark.dev
    Rejected: non-https, foreign hosts, proto-relative (//…), backslash tricks,
    relative paths, None/empty.
    """
    if not url or not isinstance(url, str):
        return False
    # Reject proto-relative and backslash-based bypass attempts before parsing.
    if url.startswith("//") or "\\" in url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.netloc.lower()
    # Strip port (e.g. "heylark.dev:443").
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    if host == _ALLOWED_BARE_HOST:
        return True
    # e.g. "jobs.heylark.dev", "login.heylark.dev"
    if host.endswith(_ALLOWED_HOST_SUFFIX):
        return True
    return False


# --- Settings dependency ----------------------------------------------------


@dataclass
class LoginSettings:
    """Auth knobs for the login service. All from env, nothing hardcoded."""
    session_secret: str
    login_bot_token: str
    login_bot_username: str
    cookie_domain: str
    public_url: str = ""  # https://login.heylark.dev; for absolute callback URL


def get_config() -> Config:
    return load_config()


def get_login_settings(config: Config = Depends(get_config)) -> LoginSettings:
    return LoginSettings(
        session_secret=config.session_secret or "",
        login_bot_token=config.tg_login_bot_token or "",
        login_bot_username=config.tg_login_bot_username or "",
        cookie_domain=config.cookie_domain,
        public_url=config.login_public_url,
    )


# --- App factory ------------------------------------------------------------


def create_app() -> FastAPI:
    _app = FastAPI(title="Heylark Login", version="1.0.0")

    @_app.get("/", response_class=HTMLResponse)
    def login_page(
        request: Request,
        next: Optional[str] = None,
        settings: LoginSettings = Depends(get_login_settings),
    ) -> HTMLResponse:
        """Render the Telegram Login Widget page.

        Accepts an optional ``?next=<url>`` query param (validated on redirect).
        The widget's data-auth-url is absolute when LOGIN_PUBLIC_URL is set
        (required for mobile login via oauth.telegram.org).
        """
        base = settings.public_url.rstrip("/")
        callback_base = f"{base}/auth/callback" if base else "/auth/callback"
        if next:
            callback_url = f"{callback_base}?next={quote(next, safe='')}"
        else:
            callback_url = callback_base
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "bot_username": settings.login_bot_username,
                "callback_url": callback_url,
            },
        )

    @_app.get("/auth/callback")
    def auth_callback(
        request: Request,
        settings: LoginSettings = Depends(get_login_settings),
    ) -> RedirectResponse:
        """Telegram Login Widget redirect target.

        Verifies the HMAC-signed widget payload, mints a session cookie, then
        303-redirects to the validated ``next`` (or DEFAULT_POST_LOGIN).
        No grant/authorization check here — that is app-specific and done by
        each app that reads the cookie.
        """
        params = dict(request.query_params)
        remember = params.pop("remember", "0") in ("1", "true", "True", "on")
        next_url = params.pop("next", None)

        user = tg_auth.verify_login_widget(params, settings.login_bot_token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid Telegram login")

        token, max_age = tg_auth.issue_session(
            user["id"], secret=settings.session_secret, remember=remember
        )
        redirect_to = (
            next_url if (next_url and is_safe_next(next_url)) else DEFAULT_POST_LOGIN
        )
        response = RedirectResponse(url=redirect_to, status_code=303)
        tg_auth.set_session_cookie(response, token, max_age, settings.cookie_domain)
        return response

    return _app


app = create_app()

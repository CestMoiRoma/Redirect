"""FastAPI application: the redirect route, the JSON API and the static front end."""

import asyncio
import html
import logging
import mimetypes
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .auth import (
    API_KEY_CREATED_AT,
    API_KEY_HASH,
    API_KEY_HINT,
    API_KEY_HINT_LENGTH,
    API_KEY_PREFIX,
    LOGIN_WINDOW_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    SESSION_SECRET,
    LoginThrottle,
    api_key_matches,
    bearer_token,
    check_password,
    hash_api_key,
    make_session,
    new_api_key_body,
    new_secret,
    session_is_valid,
)
from .config import Settings
from .db import Clock, CodeMaker, Database, Redirect, SlugTakenError, new_code, timestamp, utc_now
from .mdns import Announcer, ZeroconfAnnouncer
from .models import (
    DEFAULT_HOSTNAME,
    LINKS_PREFIX,
    REDIRECT_PREFIX,
    ApiKeyOut,
    AuthOut,
    ErrorOut,
    LoginIn,
    NewApiKeyOut,
    RedirectIn,
    RedirectList,
    RedirectOut,
    SettingsIn,
    SettingsOut,
    is_valid_code,
    is_valid_slug,
    public_url,
    short_url,
    validate_target,
)
from .strings import t

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Template of the server-rendered pages (404, 503)
PAGE_TEMPLATE = STATIC_DIR / "page.html"
# Python < 3.13 does not know the web font type: StaticFiles would serve application/octet-stream
mimetypes.add_type("font/woff2", ".woff2")
# Longest path echoed back in the logs
MAX_ECHOED_PATH = 300
# Largest request body: a redirect is at most 255 + 100 + 2048 characters, JSON escaping included this is plenty
MAX_BODY_BYTES = 16 * 1024

# Pages and API answers are same-origin only; nothing inline, nothing external
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self';"
        " base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# Fields of RedirectIn, whose messages come from models.py
FIELDS = ("slug", "name", "target")
# Types of the PydanticCustomError raised by models.py: their message is the one to show
CUSTOM_ERRORS = tuple(f"{name}_" for name in (*FIELDS, "hostname"))
# Names in the settings table, next to those of auth.py
HOSTNAME = "hostname"
HOSTNAME_ENABLED = "hostname_enabled"
API_ENABLED = "api_enabled"
# Routes only the admin session may use, never an API key
SESSION_ONLY = ("/api/key", "/api/settings")

logger = logging.getLogger("redirect")

_TEMPLATE_KEY = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _error(status: int, detail: str, errors: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorOut(detail=detail, errors=errors or {}).model_dump(), status_code=status)


def _field_message(field: str, error_type: str, message: str) -> str:
    """Message for one invalid body field: ours when the model raised it, a generic one otherwise."""
    if error_type == "missing" and field in FIELDS:
        return t(f"errors.{field}_required")
    if error_type.startswith(CUSTOM_ERRORS):
        return message
    return t("errors.value_invalid")


async def _validation_error(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    errors: dict[str, str] = {}
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        error_type = str(error.get("type", ""))
        if error_type == "json_invalid" or loc == ("body",):
            return _error(422, t("errors.body_invalid"))
        if len(loc) >= 2 and loc[0] == "body":
            field = str(loc[1])
            errors.setdefault(field, _field_message(field, error_type, str(error.get("msg", ""))))
    return _error(422, t("errors.fields_invalid"), errors)


HEALTH_PATH = "/api/health"


class HealthCheckFilter(logging.Filter):
    """Drops uvicorn's access line for the health check: the container healthcheck calls it every 30 s."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs '%s - "%s %s HTTP/%s" %d' with (client, method, path, http version, status)
        args = record.args
        return not (isinstance(args, tuple) and len(args) == 5 and args[2] == HEALTH_PATH)


def configure_logging(level: str) -> None:
    """One line per event on stderr, under the "redirect" logger (uvicorn keeps its own loggers)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthCheckFilter) for f in access.filters):
        access.addFilter(HealthCheckFilter())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level)


def render_page(title: str, text: str) -> str:
    """A server-rendered page: texts come from strings.json, every inserted value is HTML-escaped."""
    values = {"title": title, "text": text, "back": t("page.back")}
    template = PAGE_TEMPLATE.read_text(encoding="utf-8")
    return _TEMPLATE_KEY.sub(lambda m: html.escape(values[m.group(1)]), template)


def _route_path(request: Request) -> str:
    """The decoded request path, without the root path the app may be mounted under."""
    return str(request.scope["path"]).removeprefix(request.scope.get("root_path", ""))


def _is_redirect_path(path: str) -> bool:
    """The public links: /redirect/<slug> and /links/<id>."""
    return path in ("/redirect", "/links") or path.startswith((REDIRECT_PREFIX, LINKS_PREFIX))


def not_found_page(path: str) -> HTMLResponse:
    """The 404 page of a public link. The path is shown only when it is a valid link: never arbitrary text."""
    if path.startswith(LINKS_PREFIX):
        code = path.removeprefix(LINKS_PREFIX)
        logger.info("link id=%r status=404", code[:MAX_ECHOED_PATH])
        valid = is_valid_code(code)
    else:
        slug = path.removeprefix(REDIRECT_PREFIX)
        logger.info("redirect slug=%r status=404", slug[:MAX_ECHOED_PATH])
        valid = path.startswith(REDIRECT_PREFIX) and is_valid_slug(slug)
    text = t("notfound.text", path=path) if valid else t("notfound.text_unknown")
    return HTMLResponse(render_page(t("notfound.title"), text), status_code=404, headers={"Cache-Control": "no-store"})


async def _http_error(request: Request, exc: Exception) -> Response:
    """Routing errors keep FastAPI's answer, except a 404 under /redirect, which is the HTML page like any other."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 413:
        return _error(413, t("errors.body_too_large"))
    path = _route_path(request)
    if exc.status_code == 404 and _is_redirect_path(path):
        return not_found_page(path)
    return await http_exception_handler(request, exc)


async def _database_error(request: Request, exc: Exception) -> Response:
    """The database is locked, gone or broken: 503, as a page for the public links, as JSON for the API."""
    path = _route_path(request)
    logger.error("database error on %r: %s", path[:MAX_ECHOED_PATH], exc)
    headers = {"Cache-Control": "no-store", "Retry-After": "5"}
    if _is_redirect_path(path):
        page = render_page(t("unavailable.title"), t("unavailable.text"))
        return HTMLResponse(page, status_code=503, headers=headers)
    response = _error(503, t("errors.database_unavailable"))
    response.headers.update(headers)
    return response


class BodySizeLimit:
    """Refuses a request body over MAX_BODY_BYTES with 413, before it is read into memory."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await _error(413, t("errors.body_too_large"))(scope, receive, send)
            return
        received = 0

        async def counting_receive() -> Message:
            # A chunked body has no length up front: count it as it arrives
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise StarletteHTTPException(413)
            return message

        await self.app(scope, counting_receive, send)


# What an API key may call: everything that reads or changes redirects
KEY_API_PREFIXES = ("/api/redirects", "/api/list")


def _is_redirects_api(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in KEY_API_PREFIXES)


def _unauthorized(message: str) -> JSONResponse:
    response = _error(401, message)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


class _State:
    """Loaded at startup, then kept in memory: read on every request without a database round trip."""

    session_secret = ""
    api_key_hash: str | None = None
    hostname = DEFAULT_HOSTNAME
    hostname_enabled = True
    # The interface's choice; API_ENABLED, when set, overrides it
    api_enabled = False
    # The hostname is announced on the LAN (mDNS)
    mdns = False


def create_app(
    settings: Settings | None = None,
    clock: Clock = utc_now,
    new_key: Callable[[], str] = new_api_key_body,
    announcer: Announcer | None = None,
    make_code: CodeMaker = new_code,
) -> FastAPI:
    """The app. `clock`, `new_key`, `make_code` and `announcer` are injected so tests and screenshots can freeze
    time, keys and ids, and stay off the network."""
    settings = settings or Settings.from_env()
    if settings.public_base_url is not None:
        try:
            validate_target(settings.public_base_url)
        except PydanticCustomError:
            raise ValueError(
                f"PUBLIC_BASE_URL must be an http:// or https:// URL, got {settings.public_base_url!r}"
            ) from None
    configure_logging(settings.log_level)
    db = Database(settings.db_path, clock, make_code)
    password = settings.admin_password
    auth = _State()
    throttle = LoginThrottle()
    mdns = announcer if announcer is not None else ZeroconfAnnouncer(settings.port)

    def announced_name() -> str | None:
        return auth.hostname if auth.hostname_enabled else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.migrate()
        auth.session_secret = db.setting_or_create(SESSION_SECRET, new_secret)
        auth.api_key_hash = db.setting(API_KEY_HASH)
        auth.hostname = db.setting(HOSTNAME) or DEFAULT_HOSTNAME
        auth.hostname_enabled = db.setting(HOSTNAME_ENABLED) != "0"
        auth.api_enabled = db.setting(API_ENABLED) == "1"
        # In a thread: announcing probes the network for about a second
        auth.mdns = await asyncio.to_thread(mdns.announce, announced_name())
        if password is None:
            logger.warning("ADMIN_PASSWORD is not set: anyone who can reach Redirect can change its redirects")
        logger.info("Redirect %s ready, database %s", __version__, settings.db_path)
        yield
        await asyncio.to_thread(mdns.close)
        logger.info("Redirect stopped")

    app = FastAPI(
        title="Redirect",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.db = db
    app.state.throttle = throttle
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(sqlite3.Error, _database_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    # Added before the two middlewares below, so it runs inside them: a 413 carries the security headers too
    app.add_middleware(BodySizeLimit)

    def api_enabled() -> bool:
        return settings.api_enabled if settings.api_enabled is not None else auth.api_enabled

    def logged_in(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE)
        if password is None or token is None:
            return False
        return session_is_valid(token, auth.session_secret, password, clock().timestamp())

    @app.middleware("http")
    async def require_login(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """With a password, the redirects API needs the admin session or the API key; the key API, the session."""
        path = _route_path(request)
        if password is None or logged_in(request):
            return await call_next(request)
        if path in SESSION_ONLY:
            return _unauthorized(t("errors.session_required"))
        if _is_redirects_api(path):
            key = bearer_token(request.headers.get("authorization", ""))
            if key is None:
                return _unauthorized(t("errors.login_required"))
            if not api_enabled():
                return _error(403, t("errors.api_disabled"))
            if not api_key_matches(key, auth.api_key_hash):
                return _unauthorized(t("errors.api_key_invalid"))
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/static/"):
            # Revalidate on every load (a 304 on a LAN): after an upgrade, the page never mixes old and new files
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def base_url(request: Request) -> str:
        """PUBLIC_BASE_URL when set, else the hostname of the settings, else what the browser used (Host header)."""
        if settings.public_base_url:
            return settings.public_base_url
        if auth.hostname_enabled:
            scheme = request.url.scheme
            port = "" if scheme == "https" or settings.port == 80 else f":{settings.port}"
            return f"{scheme}://{auth.hostname}{port}"
        return str(request.base_url).rstrip("/")

    def out(redirect: Redirect, request: Request) -> RedirectOut:
        base = base_url(request)
        return RedirectOut(
            id=redirect.code,
            slug=redirect.slug,
            name=redirect.name,
            target=redirect.target,
            public_url=public_url(base, redirect.slug),
            short_url=short_url(base, redirect.code),
            created_at=redirect.created_at,
            updated_at=redirect.updated_at,
        )

    # --- Redirects ---------------------------------------------------------------------------------------

    @app.api_route("/links", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/redirect", methods=["GET", "HEAD"], include_in_schema=False)
    def redirect_root(request: Request) -> Response:
        return not_found_page(_route_path(request))

    @app.api_route("/redirect/{slug:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def follow(request: Request) -> Response:
        # Read the slug from the path itself: the route pattern ends with "$", which also matches before a final
        # newline, so its parameter would turn "ok\n" into "ok"
        path = _route_path(request)
        slug = path.removeprefix(REDIRECT_PREFIX)
        target = db.target_for(slug) if is_valid_slug(slug) else None
        if target is None:
            return not_found_page(path)
        # The target is logged nowhere: it may carry a token
        logger.info("redirect slug=%s status=302", slug)
        # no-store: a changed target must take effect on the very next click
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    @app.api_route("/links/{code:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def follow_short(request: Request) -> Response:
        path = _route_path(request)
        code = path.removeprefix(LINKS_PREFIX)
        target = db.target_for_code(code) if is_valid_code(code) else None
        if target is None:
            return not_found_page(path)
        logger.info("link id=%s status=302", code)
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    # --- API -----------------------------------------------------------------------------------------------

    @app.get(HEALTH_PATH)
    def health() -> Response:
        try:
            db.ping()
        except Exception:
            logger.exception("Health check failed")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/auth")
    def auth_status(request: Request) -> AuthOut:
        return AuthOut(login_required=password is not None, logged_in=logged_in(request))

    @app.post("/api/login", status_code=204, responses={401: {"model": ErrorOut}, 429: {"model": ErrorOut}})
    def login(body: LoginIn, request: Request) -> Response:
        if password is None:
            return Response(status_code=204)
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            logger.warning("login refused for %s: too many failed attempts", client)
            refused = _error(429, t("errors.too_many_attempts"))
            refused.headers["Retry-After"] = str(LOGIN_WINDOW_SECONDS)
            return refused
        if not check_password(body.password, password):
            throttle.failed(client)
            logger.warning("failed login from %s", client)
            return _error(401, t("errors.password_wrong"))
        throttle.succeeded(client)
        logger.info("login from %s", client)
        response = Response(status_code=204)
        response.set_cookie(
            SESSION_COOKIE,
            make_session(auth.session_secret, password, clock().timestamp()),
            max_age=SESSION_SECONDS,
            path="/",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/api/logout", status_code=204)
    def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    def key_disabled() -> JSONResponse | None:
        # Without a password the API is open: a key would protect nothing, and one planted now would outlive
        # the day a password is set
        return _error(409, t("errors.key_needs_password")) if password is None else None

    @app.get("/api/key", response_model=ApiKeyOut, responses={409: {"model": ErrorOut}})
    def api_key_status() -> Response | ApiKeyOut:
        if (disabled := key_disabled()) is not None:
            return disabled
        hint = db.setting(API_KEY_HINT)
        return ApiKeyOut(exists=hint is not None, hint=hint, created_at=db.setting(API_KEY_CREATED_AT))

    @app.post("/api/key", status_code=201, response_model=NewApiKeyOut, responses={409: {"model": ErrorOut}})
    def regenerate_api_key(response: Response) -> Response | NewApiKeyOut:
        if (disabled := key_disabled()) is not None:
            return disabled
        if not api_enabled():
            return _error(409, t("errors.api_disabled_key"))
        key = API_KEY_PREFIX + new_key()
        hint = key[:API_KEY_HINT_LENGTH]
        created_at = timestamp(clock())
        key_hash = hash_api_key(key)
        db.set_settings({API_KEY_HASH: key_hash, API_KEY_HINT: hint, API_KEY_CREATED_AT: created_at})
        auth.api_key_hash = key_hash
        logger.info("API key regenerated, it starts with %s", hint)
        # The only response that ever carries the key: no cache may keep it
        response.headers["Cache-Control"] = "no-store"
        return NewApiKeyOut(key=key, hint=hint, created_at=created_at)

    def settings_out() -> SettingsOut:
        return SettingsOut(
            hostname=auth.hostname,
            hostname_enabled=auth.hostname_enabled,
            public_base_url=settings.public_base_url,
            api_enabled=api_enabled(),
            api_forced=settings.api_enabled is not None,
            mdns=auth.mdns,
        )

    @app.get("/api/settings")
    def get_settings() -> SettingsOut:
        return settings_out()

    @app.put("/api/settings", response_model=SettingsOut, responses={409: {"model": ErrorOut}})
    def put_settings(body: SettingsIn) -> Response | SettingsOut:
        if settings.api_enabled is not None and body.api_enabled != settings.api_enabled:
            return _error(409, t("errors.api_forced"))
        values = {HOSTNAME: body.hostname, HOSTNAME_ENABLED: "1" if body.hostname_enabled else "0"}
        if settings.api_enabled is None:
            values[API_ENABLED] = "1" if body.api_enabled else "0"
        db.set_settings(values)
        name_changed = (body.hostname, body.hostname_enabled) != (auth.hostname, auth.hostname_enabled)
        auth.hostname, auth.hostname_enabled = body.hostname, body.hostname_enabled
        if name_changed:
            auth.mdns = mdns.announce(announced_name())
        if settings.api_enabled is None:
            auth.api_enabled = body.api_enabled
        logger.info(
            "settings: hostname %s (%s), API %s",
            body.hostname,
            "on" if body.hostname_enabled else "off",
            "on" if api_enabled() else "off",
        )
        return settings_out()

    @app.get("/api/list")
    @app.get("/api/redirects")
    def list_redirects(request: Request) -> RedirectList:
        return RedirectList(base_url=base_url(request), redirects=[out(r, request) for r in db.list_all()])

    @app.post("/api/redirects", status_code=201, response_model=RedirectOut, responses={409: {"model": ErrorOut}})
    def create_redirect(body: RedirectIn, request: Request) -> Response | RedirectOut:
        try:
            created = db.create(body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        logger.info("created id=%s slug=%s", created.code, created.slug)
        return out(created, request)

    @app.put("/api/redirects/{redirect_id}", response_model=RedirectOut, responses={404: {"model": ErrorOut}})
    def update_redirect(redirect_id: str, body: RedirectIn, request: Request) -> Response | RedirectOut:
        try:
            updated = db.update(redirect_id, body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        if updated is None:
            return _error(404, t("errors.not_found"))
        logger.info("updated id=%s slug=%s", updated.code, updated.slug)
        return out(updated, request)

    @app.delete("/api/redirects/{redirect_id}", status_code=204, responses={404: {"model": ErrorOut}})
    def delete_redirect(redirect_id: str) -> Response:
        deleted = db.delete(redirect_id)
        if deleted is None:
            return _error(404, t("errors.not_found"))
        logger.info("deleted id=%s slug=%s", deleted.code, deleted.slug)
        return Response(status_code=204)

    # --- Front end -----------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

"""FastAPI application: the redirect route, the JSON API and the static front end."""

import asyncio
import html
import ipaddress
import time
import logging
import mimetypes
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
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
from .qrcode import render_svg
from .models import (
    DEFAULT_HOSTNAME,
    LINKS_PREFIX,
    NANO_SECTION,
    QRCODE_SECTION,
    REDIRECT_PREFIX,
    ApiKeyOut,
    AuthOut,
    ErrorOut,
    EventOut,
    LoginIn,
    LogsOut,
    NanoIn,
    QRDesign,
    QRIn,
    QRList,
    QROut,
    QRPreviewIn,
    NewApiKeyOut,
    RedirectIn,
    RedirectList,
    RedirectOut,
    SettingsIn,
    SettingsOut,
    SvgOut,
    is_valid_code,
    is_valid_slug,
    public_url,
    short_url,
    slug_from_name,
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
# ...except for the QR codes, whose design may carry a logo of 256 KiB, base64-encoded
MAX_QR_BODY_BYTES = 512 * 1024
QR_API = "/api/qrcodes"

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
CUSTOM_ERRORS = tuple(f"{name}_" for name in (*FIELDS, "hostname", "design"))
# Names in the settings table, next to those of auth.py
HOSTNAME = "hostname"
HOSTNAME_ENABLED = "hostname_enabled"
API_ENABLED = "api_enabled"
LOCAL_ONLY = "local_only"
# What stays open to any address when Redirect only answers on the local network: the links it gives out, the
# container's health check, and the files the 403 page needs
PUBLIC_PATHS = ("/redirect", "/links", "/api/health", "/static/")
# The local network, spelled out (ipaddress's is_private also counts documentation and benchmark ranges):
# private ranges, loopback, link-local, and 100.64.0.0/10, the shared space of Tailscale and similar VPNs
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
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


# The page's link to the interface, left out where the interface is out of reach
_BACK_LINK = re.compile(r"\s*<a [^>]*>\{\{ back \}\}</a>")


def render_page(title: str, text: str, link: bool = True) -> str:
    """A server-rendered page: texts come from strings.json, every inserted value is HTML-escaped."""
    values = {"title": title, "text": text, "back": t("page.back")}
    template = PAGE_TEMPLATE.read_text(encoding="utf-8")
    if not link:
        template = _BACK_LINK.sub("", template)
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
        limit = MAX_QR_BODY_BYTES if str(scope.get("path", "")).startswith(QR_API) else MAX_BODY_BYTES
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _error(413, t("errors.body_too_large"))(scope, receive, send)
            return
        received = 0

        async def counting_receive() -> Message:
            # A chunked body has no length up front: count it as it arrives
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise StarletteHTTPException(413)
            return message

        await self.app(scope, counting_receive, send)


# What an API key may call: everything that reads or changes redirects
KEY_API_PREFIXES = ("/api/redirects", "/api/list", "/api/logs", "/api/nanourls", "/api/qrcodes")
# Filters of GET /api/logs and the actions they keep
LOG_FILTERS = {"visits": ("visit",), "changes": ("create", "update", "delete"), "reads": ("read",)}
# The log is purged at startup, then at most this often
PURGE_EVERY_SECONDS = 3600
# Longest user agent kept in the log
MAX_USER_AGENT = 300


def is_local_address(host: str | None) -> bool:
    """An address of LOCAL_NETWORKS (an IPv4 address mapped in IPv6 counts as itself). A client without a
    readable address is not local."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in LOCAL_NETWORKS if network.version == address.version)


def ip_base_url(scheme: str, host: str, port: int) -> str:
    """http://<address>[:port]: the port only when it is not the scheme's own, IPv6 addresses in brackets."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


def _is_public_path(path: str) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in PUBLIC_PATHS)


def _is_redirects_api(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in KEY_API_PREFIXES)


def _unauthorized(message: str) -> JSONResponse:
    response = _error(401, message)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def issue_api_key(db: Database, clock: Clock, new_key: Callable[[], str]) -> NewApiKeyOut:
    """A new API key, stored as its hash (the old one stops working): the only time the key itself exists."""
    key = API_KEY_PREFIX + new_key()
    hint = key[:API_KEY_HINT_LENGTH]
    created_at = timestamp(clock())
    db.set_settings({API_KEY_HASH: hash_api_key(key), API_KEY_HINT: hint, API_KEY_CREATED_AT: created_at})
    logger.info("API key regenerated, it starts with %s", hint)
    return NewApiKeyOut(key=key, hint=hint, created_at=created_at)


class _State:
    """Loaded at startup, then kept in memory: read on every request without a database round trip."""

    session_secret = ""
    api_key_hash: str | None = None
    hostname = DEFAULT_HOSTNAME
    hostname_enabled = True
    # The interface's choice; API_ENABLED, when set, overrides it
    api_enabled = False
    # The interface's choice; LOCAL_ONLY, when set, overrides it
    local_only = True
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

    last_purge = [0.0]

    def purge_log() -> None:
        last_purge[0] = time.monotonic()
        removed = db.purge_events(clock() - timedelta(days=settings.log_retention_days))
        if removed:
            logger.info("log: %d events older than %d days purged", removed, settings.log_retention_days)

    def source_of(request: Request) -> str:
        """api when a script sent the API key (the middleware has checked it), interface otherwise."""
        return "api" if bearer_token(request.headers.get("authorization", "")) else "interface"

    def record(
        action: str,
        request: Request,
        source: str | None = None,
        redirect: Redirect | None = None,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        """Adds an event to the log. A failure is logged and never breaks the request."""
        try:
            db.log_event(
                action,
                source or source_of(request),
                redirect,
                path,
                status,
                request.client.host if request.client else None,
                (request.headers.get("user-agent") or "")[:MAX_USER_AGENT] or None,
            )
            if time.monotonic() - last_purge[0] > PURGE_EVERY_SECONDS:
                purge_log()
        except sqlite3.Error as exc:
            logger.warning("log: cannot record a %s event: %s", action, exc)

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
        auth.local_only = db.setting(LOCAL_ONLY) != "0"
        if settings.headless:
            if auth.api_key_hash is None:
                # Without an interface, the logs are the only place to hand the first key over
                issued = issue_api_key(db, clock, new_key)
                auth.api_key_hash = db.setting(API_KEY_HASH)
                logger.warning("HEADLESS: API key created, shown this once: %s", issued.key)
            logger.info("HEADLESS: no interface; manage the redirects through the API, with the API key")
        purge_log()
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

    def local_only() -> bool:
        return settings.local_only if settings.local_only is not None else auth.local_only

    def api_enabled() -> bool:
        if settings.headless:
            return True
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
        # Headless, the API key is the only way in, password or not
        if (password is None and not settings.headless) or logged_in(request):
            return await call_next(request)
        if path in SESSION_ONLY:
            return _unauthorized(t("errors.session_required"))
        if _is_redirects_api(path):
            key = bearer_token(request.headers.get("authorization", ""))
            if key is None:
                return _unauthorized(t("errors.key_required") if settings.headless else t("errors.login_required"))
            if not api_enabled():
                return _error(403, t("errors.api_disabled"))
            if not api_key_matches(key, auth.api_key_hash):
                # A key issued by `python -m redirect api-key` while running: read the stored one again
                auth.api_key_hash = db.setting(API_KEY_HASH)
                if not api_key_matches(key, auth.api_key_hash):
                    return _unauthorized(t("errors.api_key_invalid"))
        return await call_next(request)

    @app.middleware("http")
    async def only_local(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Runs before the login check: an address outside the local network learns nothing but a 403."""
        path = _route_path(request)
        if (
            not local_only()
            or _is_public_path(path)
            or is_local_address(request.client.host if request.client else None)
        ):
            return await call_next(request)
        logger.warning("refused %s from %s: not on the local network", path[:MAX_ECHOED_PATH], request.client)
        if path.startswith("/api/"):
            return _error(403, t("errors.local_only"))
        # No link to the interface: from there, it would only lead to this page again
        return HTMLResponse(render_page(t("denied.title"), t("denied.text"), link=False), status_code=403)

    def running(status: int) -> PlainTextResponse:
        return PlainTextResponse(t("headless.running"), status_code=status, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def headless(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """HEADLESS: the links and the API (with the key) answer; anything else is one line of plain text,
        no page, no style sheet, no JSON. It wraps the local-only check, so a refusal is that line too."""
        if not settings.headless:
            return await call_next(request)
        path = _route_path(request)
        api = path == "/api/health" or (_is_redirects_api(path) and path != f"{QR_API}/preview")
        if not (_is_redirect_path(path) or api):
            return running(200 if path == "/" else 404)
        response = await call_next(request)
        # A link that leads nowhere, a refusal: the status stays, the page goes
        if response.headers.get("content-type", "").startswith("text/html"):
            return running(response.status_code)
        return response

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

    def ip_base(request: Request) -> str:
        """Redirect's own address, the one this request reached (the server socket's), on PORT. It works where
        no name resolves. PUBLIC_BASE_URL wins: behind a reverse proxy, that address is an internal one."""
        if settings.public_base_url:
            return settings.public_base_url
        server = request.scope.get("server")
        host = str(server[0]) if server else (request.url.hostname or "localhost")
        return ip_base_url(request.url.scheme, host, settings.port)

    def base_of(redirect: Redirect, request: Request) -> str:
        """NanoURLs and QR codes use the IP address unless they chose the hostname; other redirects, the
        hostname of the settings."""
        if redirect.kind != "link" and redirect.link_host == "ip":
            return ip_base(request)
        return base_url(request)

    def out(redirect: Redirect, request: Request) -> RedirectOut:
        base = base_of(redirect, request)
        return RedirectOut(
            kind=redirect.kind,
            link_host=None if redirect.kind == "link" else redirect.link_host,
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
            record("visit", request, "link", path=path[:MAX_ECHOED_PATH], status=404)
            return not_found_page(path)
        # The target is logged nowhere: it may carry a token
        logger.info("redirect slug=%s status=302", slug)
        record("visit", request, "link", path=path, status=302)
        # no-store: a changed target must take effect on the very next click
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    @app.api_route("/links/{code:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def follow_short(request: Request) -> Response:
        path = _route_path(request)
        code = path.removeprefix(LINKS_PREFIX)
        target = db.target_for_code(code) if is_valid_code(code) else None
        if target is None:
            record("visit", request, "link", path=path[:MAX_ECHOED_PATH], status=404)
            return not_found_page(path)
        logger.info("link id=%s status=302", code)
        record("visit", request, "link", path=path, status=302)
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
        issued = issue_api_key(db, clock, new_key)
        auth.api_key_hash = hash_api_key(issued.key)
        # The only response that ever carries the key: no cache may keep it
        response.headers["Cache-Control"] = "no-store"
        return issued

    def settings_out() -> SettingsOut:
        return SettingsOut(
            hostname=auth.hostname,
            hostname_enabled=auth.hostname_enabled,
            public_base_url=settings.public_base_url,
            api_enabled=api_enabled(),
            api_forced=settings.api_enabled is not None,
            mdns=auth.mdns,
            local_only=local_only(),
            local_only_forced=settings.local_only is not None,
        )

    @app.get("/api/settings")
    def get_settings() -> SettingsOut:
        return settings_out()

    @app.put("/api/settings", response_model=SettingsOut, responses={409: {"model": ErrorOut}})
    def put_settings(body: SettingsIn) -> Response | SettingsOut:
        if settings.api_enabled is not None and body.api_enabled != settings.api_enabled:
            return _error(409, t("errors.api_forced"))
        if settings.local_only is not None and body.local_only not in (None, settings.local_only):
            return _error(409, t("errors.local_only_forced"))
        values = {HOSTNAME: body.hostname, HOSTNAME_ENABLED: "1" if body.hostname_enabled else "0"}
        if settings.api_enabled is None:
            values[API_ENABLED] = "1" if body.api_enabled else "0"
        if settings.local_only is None and body.local_only is not None:
            values[LOCAL_ONLY] = "1" if body.local_only else "0"
        db.set_settings(values)
        name_changed = (body.hostname, body.hostname_enabled) != (auth.hostname, auth.hostname_enabled)
        auth.hostname, auth.hostname_enabled = body.hostname, body.hostname_enabled
        if name_changed:
            auth.mdns = mdns.announce(announced_name())
        if settings.api_enabled is None:
            auth.api_enabled = body.api_enabled
        if settings.local_only is None and body.local_only is not None:
            auth.local_only = body.local_only
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
        if source_of(request) == "api":
            record("read", request, path=_route_path(request))
        return RedirectList(
            base_url=base_url(request), ip_base_url=ip_base(request), redirects=[out(r, request) for r in db.list_all()]
        )

    @app.post("/api/redirects", status_code=201, response_model=RedirectOut, responses={409: {"model": ErrorOut}})
    def create_redirect(body: RedirectIn, request: Request) -> Response | RedirectOut:
        try:
            created = db.create(body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        logger.info("created id=%s slug=%s", created.code, created.slug)
        record("create", request, redirect=created)
        return out(created, request)

    @app.put("/api/redirects/{redirect_id}", response_model=RedirectOut, responses={404: {"model": ErrorOut}})
    def update_redirect(redirect_id: str, body: RedirectIn, request: Request) -> Response | RedirectOut:
        existing = db.get(redirect_id)
        if existing is not None and existing.kind != "link":
            return _error(409, t("errors.wrong_route"))
        try:
            updated = db.update(redirect_id, body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        if updated is None:
            return _error(404, t("errors.not_found"))
        logger.info("updated id=%s slug=%s", updated.code, updated.slug)
        record("update", request, redirect=updated)
        return out(updated, request)

    @app.delete("/api/redirects/{redirect_id}", status_code=204, responses={404: {"model": ErrorOut}})
    def delete_redirect(redirect_id: str, request: Request) -> Response:
        deleted = db.delete(redirect_id)
        if deleted is None:
            return _error(404, t("errors.not_found"))
        logger.info("deleted id=%s slug=%s", deleted.code, deleted.slug)
        record("delete", request, redirect=deleted)
        return Response(status_code=204)

    # --- NanoURLs and QR codes: redirects whose path comes from their name ---------------------------------

    def name_taken() -> JSONResponse:
        return _error(409, t("errors.name_taken"), {"name": t("errors.name_taken")})

    def named_slug(section: str, name: str) -> str | JSONResponse:
        try:
            return slug_from_name(section, name)
        except PydanticCustomError as exc:
            return _error(422, t("errors.fields_invalid"), {"name": exc.message()})

    def create_named(
        section: str, kind: str, body: NanoIn, design: str | None, request: Request
    ) -> Response | Redirect:
        name, target = body.name, body.target
        slug = named_slug(section, name)
        if isinstance(slug, JSONResponse):
            return slug
        try:
            created = db.create(slug, name, target, kind, design, body.link_host)
        except SlugTakenError:
            return name_taken()
        logger.info("created %s id=%s slug=%s", kind, created.code, created.slug)
        record("create", request, redirect=created)
        return created

    def update_named(
        code: str, section: str, kind: str, body: NanoIn, design: str | None, request: Request
    ) -> Response | Redirect:
        name, target = body.name, body.target
        existing = db.get(code)
        if existing is None or existing.kind != kind:
            return _error(404, t("errors.not_found"))
        slug = named_slug(section, name)
        if isinstance(slug, JSONResponse):
            return slug
        try:
            updated = db.update(code, slug, name, target, design, body.link_host)
        except SlugTakenError:
            return name_taken()
        if updated is None:
            return _error(404, t("errors.not_found"))
        logger.info("updated %s id=%s slug=%s", kind, updated.code, updated.slug)
        record("update", request, redirect=updated)
        return updated

    def delete_named(code: str, kind: str, request: Request) -> Response:
        existing = db.get(code)
        if existing is None or existing.kind != kind:
            return _error(404, t("errors.not_found"))
        return delete_redirect(code, request)

    def list_of(kind: str, request: Request) -> RedirectList:
        redirects = [out(r, request) for r in db.list_all() if r.kind == kind]
        return RedirectList(base_url=base_url(request), ip_base_url=ip_base(request), redirects=redirects)

    @app.get("/api/nanourls")
    def list_nanos(request: Request) -> RedirectList:
        if source_of(request) == "api":
            record("read", request, path=_route_path(request))
        return list_of("nano", request)

    @app.post("/api/nanourls", status_code=201, response_model=RedirectOut, responses={409: {"model": ErrorOut}})
    def create_nano(body: NanoIn, request: Request) -> Response | RedirectOut:
        created = create_named(NANO_SECTION, "nano", body, None, request)
        return out(created, request) if isinstance(created, Redirect) else created

    @app.put("/api/nanourls/{redirect_id}", response_model=RedirectOut, responses={404: {"model": ErrorOut}})
    def update_nano(redirect_id: str, body: NanoIn, request: Request) -> Response | RedirectOut:
        updated = update_named(redirect_id, NANO_SECTION, "nano", body, None, request)
        return out(updated, request) if isinstance(updated, Redirect) else updated

    @app.delete("/api/nanourls/{redirect_id}", status_code=204, responses={404: {"model": ErrorOut}})
    def delete_nano(redirect_id: str, request: Request) -> Response:
        return delete_named(redirect_id, "nano", request)

    def qr_out(redirect: Redirect, request: Request, with_svg: bool) -> QROut:
        design = QRDesign.model_validate_json(redirect.design or "{}")
        base = out(redirect, request)
        svg = render_svg(base.short_url, design) if with_svg else None
        return QROut(**base.model_dump(), design=design, svg=svg, svg_url=f"{QR_API}/{redirect.code}/svg")

    def qr_of(code: str) -> Redirect | None:
        redirect = db.get(code)
        return redirect if redirect is not None and redirect.kind == "qrcode" else None

    @app.get(QR_API)
    def list_qrcodes(request: Request) -> QRList:
        if source_of(request) == "api":
            record("read", request, path=_route_path(request))
        qrcodes = [qr_out(r, request, with_svg=False) for r in db.list_all() if r.kind == "qrcode"]
        return QRList(base_url=base_url(request), ip_base_url=ip_base(request), qrcodes=qrcodes)

    @app.post(QR_API, status_code=201, response_model=QROut, responses={409: {"model": ErrorOut}})
    def create_qrcode(body: QRIn, request: Request) -> Response | QROut:
        design = body.design.model_dump_json()
        created = create_named(QRCODE_SECTION, "qrcode", body, design, request)
        return qr_out(created, request, with_svg=True) if isinstance(created, Redirect) else created

    @app.post(f"{QR_API}/preview")
    def preview_qrcode(body: QRPreviewIn, request: Request) -> SvgOut:
        """The QR code a design would give, without saving it: of the QR code `id`, or of a new one."""
        existing = qr_of(body.id) if body.id else None
        code = existing.code if existing else "Preview0"  # a new id has the same length: the same QR size
        base = ip_base(request) if body.link_host == "ip" else base_url(request)
        return SvgOut(svg=render_svg(short_url(base, code), body.design))

    @app.get(f"{QR_API}/{{redirect_id}}", response_model=QROut, responses={404: {"model": ErrorOut}})
    def get_qrcode(redirect_id: str, request: Request) -> Response | QROut:
        qr = qr_of(redirect_id)
        if qr is None:
            return _error(404, t("errors.not_found"))
        if source_of(request) == "api":
            record("read", request, path=_route_path(request))
        return qr_out(qr, request, with_svg=True)

    @app.get(f"{QR_API}/{{redirect_id}}/svg", include_in_schema=False)
    def qrcode_svg(redirect_id: str, request: Request) -> Response:
        qr = qr_of(redirect_id)
        if qr is None:
            return _error(404, t("errors.not_found"))
        filename = qr.slug.rsplit("/", 1)[-1] + ".svg"
        return Response(
            qr_out(qr, request, with_svg=True).svg,
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-cache"},
        )

    @app.put(f"{QR_API}/{{redirect_id}}", response_model=QROut, responses={404: {"model": ErrorOut}})
    def update_qrcode(redirect_id: str, body: QRIn, request: Request) -> Response | QROut:
        design = body.design.model_dump_json()
        updated = update_named(redirect_id, QRCODE_SECTION, "qrcode", body, design, request)
        return qr_out(updated, request, with_svg=True) if isinstance(updated, Redirect) else updated

    @app.delete(f"{QR_API}/{{redirect_id}}", status_code=204, responses={404: {"model": ErrorOut}})
    def delete_qrcode(redirect_id: str, request: Request) -> Response:
        return delete_named(redirect_id, "qrcode", request)

    @app.get("/api/logs")
    def get_logs(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        before: int | None = Query(None, ge=1),
        kind: str | None = Query(None, pattern="^(visits|changes|reads)$"),
    ) -> LogsOut:
        if source_of(request) == "api":
            record("read", request, path=_route_path(request))
        found = db.events(limit + 1, before, LOG_FILTERS[kind] if kind else ())
        page = found[:limit]
        return LogsOut(
            events=[
                EventOut(
                    id=event.id,
                    at=event.at,
                    action=event.action,
                    source=event.source,
                    redirect_id=event.code,
                    name=event.name,
                    path=event.path,
                    status=event.status,
                    client=event.client,
                    user_agent=event.user_agent,
                )
                for event in page
            ],
            next=page[-1].id if len(found) > limit else None,
        )

    # --- Front end -----------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

"""FastAPI application: the redirect route, the JSON API and the static front end."""

import html
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic_core import PydanticCustomError

from . import __version__
from .config import Settings
from .db import Clock, Database, Redirect, SlugTakenError, utc_now
from .models import (
    ErrorOut,
    RedirectIn,
    RedirectList,
    RedirectOut,
    is_valid_slug,
    public_url,
    validate_target,
)
from .strings import t

STATIC_DIR = Path(__file__).resolve().parent / "static"
NOT_FOUND_TEMPLATE = STATIC_DIR / "404.html"
# Longest path echoed back on the 404 page and in the logs
MAX_ECHOED_PATH = 300

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

logger = logging.getLogger("redirect")

_TEMPLATE_KEY = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _error(status: int, detail: str, errors: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorOut(detail=detail, errors=errors or {}).model_dump(), status_code=status)


def _field_message(field: str, error_type: str, message: str) -> str:
    """Message for one invalid body field: ours when the model raised it, a generic one otherwise."""
    if error_type == "missing" and field in FIELDS:
        return t(f"errors.{field}_required")
    if error_type.startswith(tuple(f"{name}_" for name in FIELDS)):  # a PydanticCustomError from models.py
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


def configure_logging(level: str) -> None:
    """One line per event on stderr, under the "redirect" logger (uvicorn keeps its own loggers)."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level)


def render_not_found(path: str) -> str:
    """The 404 page: every text from strings.json, every inserted value HTML-escaped."""
    shown = path if len(path) <= MAX_ECHOED_PATH else path[:MAX_ECHOED_PATH] + "…"
    template = NOT_FOUND_TEMPLATE.read_text(encoding="utf-8")
    return _TEMPLATE_KEY.sub(lambda m: html.escape(t(m.group(1), path=shown)), template)


def create_app(settings: Settings | None = None, clock: Clock = utc_now) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.public_base_url is not None:
        try:
            validate_target(settings.public_base_url)
        except PydanticCustomError:
            raise ValueError(f"PUBLIC_BASE_URL must be an http:// or https:// URL, got {settings.public_base_url!r}") from None
    configure_logging(settings.log_level)
    db = Database(settings.db_path, clock)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.migrate()
        logger.info("Redirect %s ready, database %s", __version__, settings.db_path)
        yield
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
    app.add_exception_handler(RequestValidationError, _validation_error)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def base_url(request: Request) -> str:
        """PUBLIC_BASE_URL when set, else what the browser used to reach us (Host header)."""
        return settings.public_base_url or str(request.base_url).rstrip("/")

    def out(redirect: Redirect, request: Request) -> RedirectOut:
        return RedirectOut(public_url=public_url(base_url(request), redirect.slug), **vars(redirect))

    # --- Redirects ---------------------------------------------------------------------------------------

    def not_found_page(request: Request) -> HTMLResponse:
        return HTMLResponse(render_not_found(request.url.path), status_code=404, headers={"Cache-Control": "no-store"})

    @app.api_route("/redirect", methods=["GET", "HEAD"], include_in_schema=False)
    def redirect_root(request: Request) -> Response:
        return not_found_page(request)

    @app.api_route("/redirect/{slug:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def follow(slug: str, request: Request) -> Response:
        target = db.target_for(slug) if is_valid_slug(slug) else None
        if target is None:
            logger.info("redirect slug=%r status=404", slug[:MAX_ECHOED_PATH])
            return not_found_page(request)
        # The target is logged nowhere: it may carry a token
        logger.info("redirect slug=%s status=302", slug)
        # no-store: a changed target must take effect on the very next click
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    # --- API -----------------------------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> Response:
        try:
            db.ping()
        except Exception:
            logger.exception("Health check failed")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/redirects")
    def list_redirects(request: Request) -> RedirectList:
        return RedirectList(base_url=base_url(request), redirects=[out(r, request) for r in db.list_all()])

    @app.post("/api/redirects", status_code=201, response_model=RedirectOut, responses={409: {"model": ErrorOut}})
    def create_redirect(body: RedirectIn, request: Request) -> Response | RedirectOut:
        try:
            created = db.create(body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        logger.info("created id=%d slug=%s", created.id, created.slug)
        return out(created, request)

    @app.put("/api/redirects/{redirect_id}", response_model=RedirectOut, responses={404: {"model": ErrorOut}})
    def update_redirect(redirect_id: int, body: RedirectIn, request: Request) -> Response | RedirectOut:
        try:
            updated = db.update(redirect_id, body.slug, body.name, body.target)
        except SlugTakenError:
            return _error(409, t("errors.slug_taken"), {"slug": t("errors.slug_taken")})
        if updated is None:
            return _error(404, t("errors.not_found"))
        logger.info("updated id=%d slug=%s", updated.id, updated.slug)
        return out(updated, request)

    @app.delete("/api/redirects/{redirect_id}", status_code=204, responses={404: {"model": ErrorOut}})
    def delete_redirect(redirect_id: int) -> Response:
        deleted = db.delete(redirect_id)
        if deleted is None:
            return _error(404, t("errors.not_found"))
        logger.info("deleted id=%d slug=%s", deleted.id, deleted.slug)
        return Response(status_code=204)

    # --- Front end -----------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

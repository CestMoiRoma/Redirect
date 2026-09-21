"""API schemas. Slug, name and target validation lives here, and only here."""

import re
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError

from .strings import t

SLUG_MAX_LENGTH = 255
NAME_MAX_LENGTH = 100
TARGET_MAX_LENGTH = 2048
REDIRECT_PREFIX = "/redirect/"
LINKS_PREFIX = "/links/"
HOSTNAME_MAX_LENGTH = 253
DEFAULT_HOSTNAME = "redirect.lan"

_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
# One label of a host name (RFC 1123): letters, digits and inner dashes, 63 characters at most
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ALLOWED_SCHEMES = ("http", "https")
# Characters kept as-is when percent-encoding a non-ASCII target (RFC 3986 reserved + unreserved + "%")
_URL_SAFE = "/:@!$&'()*+,;=?#[]%-._~"


def _error(kind: str) -> PydanticCustomError:
    # The error type is the strings.json key, so the API can map it back to a field message
    return PydanticCustomError(kind, t(f"errors.{kind}"))


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def validate_slug(value: str) -> str:
    """Normalized slug, or PydanticCustomError. Rules are checked in a fixed order: one message per failure."""
    slug = value.strip()
    if not slug:
        raise _error("slug_required")
    if len(slug) > SLUG_MAX_LENGTH:
        raise _error("slug_too_long")
    segments = slug.split("/")
    if any(segment == "" for segment in segments):
        raise _error("slug_empty_segment")
    if any(segment in (".", "..") for segment in segments):
        raise _error("slug_dot_segment")
    if not all(_SEGMENT.fullmatch(segment) for segment in segments):
        raise _error("slug_invalid")
    return slug


def is_valid_slug(value: str) -> bool:
    try:
        return validate_slug(value) == value
    except PydanticCustomError:
        return False


def validate_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise _error("name_required")
    if len(name) > NAME_MAX_LENGTH:
        raise _error("name_too_long")
    if _has_control_chars(name):
        raise _error("name_invalid")
    return name


def _to_ascii_url(url: str) -> str:
    """IDNA host and percent-encoded path/query/fragment, so the URL fits in a Location header."""
    parts = urlsplit(url)
    userinfo, at, hostport = parts.netloc.rpartition("@")
    host, colon, port = hostport, "", ""
    if not hostport.startswith("[") and ":" in hostport:  # "[...]" is an IPv6 literal, ASCII already
        host, colon, port = hostport.rpartition(":")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _error("target_invalid") from None
    netloc = quote(userinfo, safe=_URL_SAFE) + at + host + colon + port
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            quote(parts.path, safe=_URL_SAFE),
            quote(parts.query, safe=_URL_SAFE),
            quote(parts.fragment, safe=_URL_SAFE),
        )
    )


def validate_target(value: str) -> str:
    """Absolute http(s) URL, returned ASCII-only. Anything else (javascript:, data:, relative…) is refused."""
    target = value.strip()
    if not target:
        raise _error("target_required")
    if len(target) > TARGET_MAX_LENGTH:
        raise _error("target_too_long")
    if _has_control_chars(target) or any(char.isspace() for char in target):
        raise _error("target_invalid")
    try:
        parts = urlsplit(target)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise _error("target_invalid") from None
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        raise _error("target_invalid")
    if not target.isascii():
        target = _to_ascii_url(target)
        if len(target) > TARGET_MAX_LENGTH:
            raise _error("target_too_long")
    return target


def validate_hostname(value: str) -> str:
    """A host name such as redirect.lan, lower-cased, without a final dot."""
    hostname = value.strip().lower().rstrip(".")
    if not hostname:
        raise _error("hostname_required")
    if len(hostname) > HOSTNAME_MAX_LENGTH or not all(_LABEL.fullmatch(label) for label in hostname.split(".")):
        raise _error("hostname_invalid")
    return hostname


def public_url(base_url: str, slug: str) -> str:
    """The link people are given: <base>/redirect/<slug>."""
    return base_url.rstrip("/") + REDIRECT_PREFIX + slug


def short_url(base_url: str, code: str) -> str:
    """The same redirect, reached by its short id: <base>/links/<id>."""
    return base_url.rstrip("/") + LINKS_PREFIX + code


_CODE = re.compile(r"[A-Za-z0-9]{1,32}")


def is_valid_code(value: str) -> bool:
    return _CODE.fullmatch(value) is not None


class RedirectIn(BaseModel):
    """Body of POST /api/redirects and PUT /api/redirects/{id}."""

    slug: str
    name: str
    target: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return validate_slug(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_name(value)

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        return validate_target(value)


class RedirectOut(BaseModel):
    # The short id: /links/<id>, and the redirect's name in the API (PUT and DELETE /api/redirects/<id>)
    id: str
    slug: str
    name: str
    target: str
    public_url: str
    short_url: str
    created_at: str
    updated_at: str


class RedirectList(BaseModel):
    """Body of GET /api/redirects. `base_url` lets the form preview the link before the entry exists."""

    base_url: str
    redirects: list[RedirectOut]


class ErrorOut(BaseModel):
    """Every API error: a message, plus one message per invalid field."""

    detail: str
    errors: dict[str, str] = {}


class LoginIn(BaseModel):
    """Body of POST /api/login."""

    password: str


class AuthOut(BaseModel):
    """Body of GET /api/auth: whether a login is required, and whether this browser has a session."""

    login_required: bool
    logged_in: bool


class ApiKeyOut(BaseModel):
    """Body of GET /api/key: never the key itself, only what tells it apart."""

    exists: bool
    hint: str | None
    created_at: str | None


class NewApiKeyOut(BaseModel):
    """Body of POST /api/key: the only response that ever carries the key."""

    key: str
    hint: str
    created_at: str


class SettingsIn(BaseModel):
    """Body of PUT /api/settings."""

    hostname: str
    hostname_enabled: bool
    api_enabled: bool

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, value: str) -> str:
        return validate_hostname(value)


class SettingsOut(BaseModel):
    """Body of GET and PUT /api/settings: the stored values, and what the environment imposes on them."""

    hostname: str
    hostname_enabled: bool
    # PUBLIC_BASE_URL, when set, makes the links and wins over the hostname
    public_base_url: str | None
    api_enabled: bool
    # True when API_ENABLED sets api_enabled: the interface cannot change it
    api_forced: bool
    # True while the hostname, ending in .local, is announced on the LAN (mDNS)
    mdns: bool

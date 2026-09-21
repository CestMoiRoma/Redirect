"""Admin login and API key: open without ADMIN_PASSWORD, guarded with it. Sessions and keys survive a restart."""

import hashlib
import itertools
import sqlite3

import pytest
from support import create

from redirect import auth
from redirect.config import Settings

PASSWORD = "correct horse battery"
LOGIN_REQUIRED = {"detail": "Log in to manage the redirects.", "errors": {}}
SESSION_REQUIRED = {"detail": "Log in to the interface to manage the API key.", "errors": {}}
KEY_INVALID = {"detail": "This API key is not valid.", "errors": {}}
WRONG_PASSWORD = {"detail": "Wrong password.", "errors": {}}
KEY_NEEDS_PASSWORD = {
    "detail": "Set ADMIN_PASSWORD to use an API key: without a login, the API is open anyway.",
    "errors": {},
}
BODY = {"slug": "wiki", "name": "Wiki", "target": "http://wiki.example/"}
REDIRECTS_API = [
    ("GET", "/api/redirects", None),
    ("POST", "/api/redirects", BODY),
    ("PUT", "/api/redirects/1", BODY),
    ("DELETE", "/api/redirects/1", None),
]


def keys():
    """Deterministic key bodies: key-1, key-2, ..."""
    counter = itertools.count(1)
    return lambda: f"key-{next(counter)}"


@pytest.fixture
def admin(make_client):
    """An app with a password, not logged in."""
    return make_client(admin_password=PASSWORD, new_key=keys())


def log_in(client, password: str = PASSWORD):
    return client.post("/api/login", json={"password": password})


@pytest.fixture
def logged_in(admin):
    assert log_in(admin).status_code == 204
    return admin


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- Without a password: open, as before ----------------------------------------------------------------


def test_without_a_password_no_login_is_required(client):
    assert client.get("/api/auth").json() == {"login_required": False, "logged_in": False}
    assert client.get("/api/redirects").status_code == 200


def test_without_a_password_login_is_a_no_op(client):
    response = log_in(client, "anything")
    assert response.status_code == 204
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_without_a_password_there_is_no_api_key(client, method):
    response = client.request(method, "/api/key")
    assert response.status_code == 409
    assert response.json() == KEY_NEEDS_PASSWORD


# --- With a password: what stays public ---------------------------------------------------------------


def test_with_a_password_a_login_is_required(admin):
    assert admin.get("/api/auth").json() == {"login_required": True, "logged_in": False}


@pytest.mark.parametrize(("method", "path", "body"), REDIRECTS_API)
def test_the_redirects_api_needs_a_login(admin, method, path, body):
    response = admin.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json() == LOGIN_REQUIRED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_login_check_comes_before_the_body(admin):
    response = admin.post("/api/redirects", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/strings.json", "/api/health", "/api/auth"])
def test_the_interface_and_the_health_check_stay_public(admin, path):
    assert admin.get(path).status_code == 200


def test_public_links_stay_public(make_client):
    open_client = make_client()
    create(open_client, "wiki", target="http://wiki.example/")
    guarded = make_client(admin_password=PASSWORD)
    response = guarded.get("/redirect/wiki", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://wiki.example/"


# --- Login, session, logout -----------------------------------------------------------------------------


def test_a_wrong_password_is_refused(admin):
    response = log_in(admin, "wrong password")
    assert response.status_code == 401
    assert response.json() == WRONG_PASSWORD
    assert "set-cookie" not in response.headers
    assert admin.get("/api/redirects").status_code == 401


def test_the_session_cookie(admin):
    response = log_in(admin)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("redirect_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=2592000" in cookie
    assert "Secure" not in cookie  # plain http: a Secure cookie would never come back


def test_the_session_cookie_is_secure_over_https(make_client):
    client = make_client(admin_password=PASSWORD, base_url="https://testserver")
    assert "Secure" in log_in(client).headers["set-cookie"]


@pytest.mark.parametrize(("method", "path", "body"), REDIRECTS_API)
def test_a_session_opens_the_redirects_api(logged_in, method, path, body):
    create(logged_in, "docs")
    assert logged_in.request(method, path, json=body).status_code in (200, 201, 204)


def test_the_session_is_reported(logged_in):
    assert logged_in.get("/api/auth").json() == {"login_required": True, "logged_in": True}


def test_logout_ends_the_session(logged_in):
    response = logged_in.post("/api/logout")
    assert response.status_code == 204
    assert 'redirect_session=""' in response.headers["set-cookie"]
    assert logged_in.get("/api/redirects").status_code == 401


@pytest.mark.parametrize("token", ["", "abc", "123.456", "9999999999.00", "x.y.z", "1" * 20 + ".00"])
def test_a_forged_session_is_refused(admin, token):
    admin.cookies.set("redirect_session", token)
    assert admin.get("/api/redirects").status_code == 401


def test_a_session_expires_after_30_days(logged_in, clock):
    clock.advance(days=30, seconds=-1)
    assert logged_in.get("/api/redirects").status_code == 200
    clock.advance(seconds=1)
    assert logged_in.get("/api/redirects").status_code == 401


def test_a_session_survives_a_restart(make_client, logged_in):
    cookie = logged_in.cookies.get("redirect_session")
    restarted = make_client(admin_password=PASSWORD)
    restarted.cookies.set("redirect_session", cookie)
    assert restarted.get("/api/redirects").status_code == 200


def test_changing_the_password_ends_every_session(make_client, logged_in):
    cookie = logged_in.cookies.get("redirect_session")
    restarted = make_client(admin_password="another password")
    restarted.cookies.set("redirect_session", cookie)
    assert restarted.get("/api/redirects").status_code == 401


# --- Too many failed logins -----------------------------------------------------------------------------


def test_repeated_failures_block_the_address_for_a_while(admin):
    moment = [1000.0]
    admin.app.state.throttle.clock = lambda: moment[0]
    for _ in range(auth.LOGIN_ATTEMPTS):
        assert log_in(admin, "wrong password").status_code == 401
    blocked = log_in(admin)  # even the right password
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many failed attempts. Try again in a few minutes.", "errors": {}}
    assert blocked.headers["retry-after"] == "900"
    moment[0] += auth.LOGIN_WINDOW_SECONDS
    assert log_in(admin).status_code == 204


def test_a_success_resets_the_failures(admin):
    for _ in range(auth.LOGIN_ATTEMPTS - 1):
        log_in(admin, "wrong password")
    assert log_in(admin).status_code == 204
    for _ in range(auth.LOGIN_ATTEMPTS - 1):
        log_in(admin, "wrong password")
    assert log_in(admin).status_code == 204


# --- API key --------------------------------------------------------------------------------------------


def test_no_key_at_first(logged_in):
    assert logged_in.get("/api/key").json() == {"exists": False, "hint": None, "created_at": None}


def test_generating_a_key_shows_it_once(logged_in):
    response = logged_in.post("/api/key")
    assert response.status_code == 201
    assert response.json() == {"key": "rdk_key-1", "hint": "rdk_key-", "created_at": "2026-01-15T09:30:00Z"}
    assert response.headers["cache-control"] == "no-store"
    status = logged_in.get("/api/key").json()
    assert status == {"exists": True, "hint": "rdk_key-", "created_at": "2026-01-15T09:30:00Z"}
    assert "key" not in status


@pytest.mark.parametrize(("method", "path", "body"), REDIRECTS_API)
def test_the_key_opens_the_redirects_api(make_client, logged_in, method, path, body):
    key = logged_in.post("/api/key").json()["key"]
    create(logged_in, "docs")
    script = make_client(admin_password=PASSWORD)
    assert script.request(method, path, json=body, headers=bearer(key)).status_code in (200, 201, 204)


def test_the_scheme_is_case_insensitive(make_client, logged_in):
    key = logged_in.post("/api/key").json()["key"]
    script = make_client(admin_password=PASSWORD)
    assert script.get("/api/redirects", headers={"Authorization": f"bearer  {key} "}).status_code == 200


@pytest.mark.parametrize("header", ["Bearer rdk_nope", "Bearer rdk_key-1x", "Basic cmRrX2tleS0x"])
def test_a_wrong_key_is_refused(make_client, logged_in, header):
    logged_in.post("/api/key")
    script = make_client(admin_password=PASSWORD)
    response = script.get("/api/redirects", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == (LOGIN_REQUIRED if header.startswith("Basic") else KEY_INVALID)


def test_any_key_is_refused_before_one_exists(admin):
    response = admin.get("/api/redirects", headers=bearer("rdk_key-1"))
    assert response.status_code == 401
    assert response.json() == KEY_INVALID


def test_regenerating_replaces_the_key_at_once(make_client, logged_in):
    first = logged_in.post("/api/key").json()["key"]
    second = logged_in.post("/api/key").json()["key"]
    assert (first, second) == ("rdk_key-1", "rdk_key-2")
    script = make_client(admin_password=PASSWORD)
    assert script.get("/api/redirects", headers=bearer(first)).status_code == 401
    assert script.get("/api/redirects", headers=bearer(second)).status_code == 200


def test_the_key_survives_a_restart(make_client, logged_in):
    key = logged_in.post("/api/key").json()["key"]
    restarted = make_client(admin_password=PASSWORD)
    assert restarted.get("/api/redirects", headers=bearer(key)).status_code == 200


def test_the_key_does_not_manage_the_key(make_client, logged_in):
    key = logged_in.post("/api/key").json()["key"]
    script = make_client(admin_password=PASSWORD)
    for method in ("GET", "POST"):
        response = script.request(method, "/api/key", headers=bearer(key))
        assert response.status_code == 401
        assert response.json() == SESSION_REQUIRED


def test_only_the_hash_of_the_key_is_stored(logged_in, tmp_path):
    key = logged_in.post("/api/key").json()["key"]
    database = tmp_path / "data" / "redirect.db"
    assert key.encode() not in database.read_bytes()
    with sqlite3.connect(database) as conn:
        stored = dict(conn.execute("SELECT name, value FROM settings").fetchall())
    assert stored["api_key_hash"] == hashlib.sha256(key.encode()).hexdigest()


def test_real_keys_are_random_and_long():
    first, second = auth.new_api_key_body(), auth.new_api_key_body()
    assert first != second
    assert len(first) >= 43  # 32 random bytes, URL-safe base64


# --- Units ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic abc", None),
        ("", None),
    ],
)
def test_bearer_token(header, token):
    assert auth.bearer_token(header) == token


def test_check_password_compares_exactly():
    assert auth.check_password(PASSWORD, PASSWORD)
    assert not auth.check_password(PASSWORD + " ", PASSWORD)
    assert not auth.check_password("", PASSWORD)


def test_sessions_are_bound_to_secret_password_and_time():
    token = auth.make_session("secret", PASSWORD, now=1000)
    assert auth.session_is_valid(token, "secret", PASSWORD, now=1000)
    assert not auth.session_is_valid(token, "other secret", PASSWORD, now=1000)
    assert not auth.session_is_valid(token, "secret", "other password", now=1000)
    assert not auth.session_is_valid(token, "secret", PASSWORD, now=1000 + auth.SESSION_SECONDS)


def test_settings_read_the_admin_password():
    assert Settings.from_env({}).admin_password is None
    assert Settings.from_env({"ADMIN_PASSWORD": "   "}).admin_password is None
    assert Settings.from_env({"ADMIN_PASSWORD": " spaced password "}).admin_password == " spaced password "


def test_settings_refuse_a_short_admin_password():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"ADMIN_PASSWORD": "short"})
    assert str(info.value) == "ADMIN_PASSWORD must be at least 8 characters long"


def test_the_admin_password_never_shows_in_a_repr():
    assert PASSWORD not in repr(Settings(admin_password=PASSWORD))

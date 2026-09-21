"""Settings kept in the volume: the hostname of the links (redirect.lan by default) and the scripts' API switch
(off by default, forced by API_ENABLED)."""

import pytest
from fastapi.testclient import TestClient
from support import PUBLIC_BASE_URL, create

from redirect.config import Settings

PASSWORD = "correct horse battery"
DEFAULTS = {
    "hostname": "redirect.lan",
    "hostname_enabled": True,
    "public_base_url": None,
    "api_enabled": False,
    "api_forced": False,
}
HOSTNAME_INVALID = "Use a hostname such as redirect.lan: letters, digits and dashes, with dots between the parts."
API_DISABLED = {"detail": "The API is turned off. Turn it on in the interface, or with API_ENABLED.", "errors": {}}


def put(client, hostname: str = "redirect.lan", hostname_enabled: bool = True, api_enabled: bool = False):
    return client.put(
        "/api/settings",
        json={"hostname": hostname, "hostname_enabled": hostname_enabled, "api_enabled": api_enabled},
    )


def log_in(client):
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return client


# --- Hostname ---------------------------------------------------------------------------------------------


def test_defaults(client):
    assert client.get("/api/settings").json() == DEFAULTS


def test_links_use_the_hostname_by_default(client):
    assert create(client, "wiki")["public_url"] == "http://redirect.lan/redirect/wiki"


def test_the_link_carries_the_port_when_it_is_not_80(make_client):
    client = make_client(port=8081)
    assert create(client, "wiki")["public_url"] == "http://redirect.lan:8081/redirect/wiki"


def test_the_link_follows_https_without_a_port(make_client):
    client = make_client(port=8081, base_url="https://testserver")
    assert create(client, "wiki")["public_url"] == "https://redirect.lan/redirect/wiki"


def test_changing_the_hostname_changes_every_link(client):
    create(client, "wiki")
    response = put(client, hostname="  Go.Example.LAN. ")
    assert response.status_code == 200
    assert response.json() == {**DEFAULTS, "hostname": "go.example.lan"}
    body = client.get("/api/redirects").json()
    assert body["base_url"] == "http://go.example.lan"
    assert body["redirects"][0]["public_url"] == "http://go.example.lan/redirect/wiki"


def test_without_a_hostname_links_follow_the_browser(client):
    put(client, hostname_enabled=False)
    body = client.get("/api/redirects", headers={"Host": "192.168.1.20"}).json()
    assert body["base_url"] == "http://192.168.1.20"


def test_public_base_url_wins_over_the_hostname(public_client):
    assert public_client.get("/api/settings").json()["public_base_url"] == PUBLIC_BASE_URL
    assert create(public_client, "wiki")["public_url"] == "http://redirect.local:8080/redirect/wiki"


@pytest.mark.parametrize(
    "hostname", ["bad_name", "-redirect.lan", "redirect-.lan", "a..lan", "x" * 64 + ".lan", "é.lan"]
)
def test_invalid_hostnames_are_refused(client, hostname):
    response = put(client, hostname=hostname)
    assert response.status_code == 422
    assert response.json() == {"detail": "Some fields are invalid.", "errors": {"hostname": HOSTNAME_INVALID}}
    assert client.get("/api/settings").json() == DEFAULTS


def test_an_empty_hostname_is_refused_even_when_disabled(client):
    response = put(client, hostname="  ", hostname_enabled=False)
    assert response.status_code == 422
    assert response.json()["errors"] == {"hostname": "Enter a hostname."}


@pytest.mark.parametrize(
    "hostname", ["redirect", "redirect.local", "go.example.org", "192.168.1.20", "a" * 63 + ".lan"]
)
def test_valid_hostnames(client, hostname):
    assert put(client, hostname=hostname).json()["hostname"] == hostname


def test_settings_survive_a_restart(make_client, client):
    put(client, hostname="links.lan", hostname_enabled=False, api_enabled=True)
    restarted = make_client()
    assert restarted.get("/api/settings").json() == {
        **DEFAULTS,
        "hostname": "links.lan",
        "hostname_enabled": False,
        "api_enabled": True,
    }


# --- The scripts' API switch ------------------------------------------------------------------------------


@pytest.fixture
def admin(make_client):
    """A password, the API left to the interface (off by default), logged in."""
    return log_in(make_client(admin_password=PASSWORD))


def test_the_api_is_off_by_default(admin):
    assert admin.get("/api/settings").json()["api_enabled"] is False
    response = admin.post("/api/key")
    assert response.status_code == 409
    assert response.json() == {"detail": "Turn the API on before generating a key.", "errors": {}}


def test_turning_the_api_on_and_off(admin):
    assert put(admin, api_enabled=True).json()["api_enabled"] is True
    key = admin.post("/api/key").json()["key"]
    # The same running app, seen from a script: no session cookie
    script = TestClient(admin.app)
    headers = {"Authorization": f"Bearer {key}"}
    assert script.get("/api/redirects", headers=headers).status_code == 200
    put(admin, api_enabled=False)
    response = script.get("/api/redirects", headers=headers)
    assert response.status_code == 403
    assert response.json() == API_DISABLED
    # The interface keeps working: the switch is for scripts only
    assert admin.get("/api/redirects").status_code == 200


def test_a_wrong_key_gets_the_same_answer_while_the_api_is_off(make_client, admin):
    script = make_client(admin_password=PASSWORD)
    response = script.get("/api/redirects", headers={"Authorization": "Bearer rdk_wrong"})
    assert response.status_code == 403
    assert response.json() == API_DISABLED


@pytest.mark.parametrize("forced", [True, False])
def test_api_enabled_in_the_environment_wins(make_client, forced):
    admin = log_in(make_client(admin_password=PASSWORD, api_enabled=forced))
    status = admin.get("/api/settings").json()
    assert (status["api_enabled"], status["api_forced"]) == (forced, True)
    refused = put(admin, api_enabled=not forced)
    assert refused.status_code == 409
    assert refused.json() == {
        "detail": "API_ENABLED in the container's environment decides whether the API is on.",
        "errors": {},
    }
    # The hostname can still change, as long as the API value matches the forced one
    assert put(admin, hostname="links.lan", api_enabled=forced).status_code == 200


def test_a_forced_value_does_not_overwrite_the_interfaces_choice(make_client, admin):
    put(admin, api_enabled=True)
    forced_off = log_in(make_client(admin_password=PASSWORD, api_enabled=False))
    put(forced_off, hostname="links.lan", api_enabled=False)
    back_to_the_interface = log_in(make_client(admin_password=PASSWORD))
    assert back_to_the_interface.get("/api/settings").json()["api_enabled"] is True


# --- Who may change the settings ----------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_settings_need_the_admin_session(make_client, method):
    client = make_client(admin_password=PASSWORD, api_enabled=True)
    response = client.request(method, "/api/settings", json=None if method == "GET" else DEFAULTS)
    assert response.status_code == 401
    assert response.json()["detail"] == "Log in to the interface to change the settings or the API key."


def test_the_api_key_cannot_change_the_settings(make_client):
    admin = log_in(make_client(admin_password=PASSWORD, api_enabled=True))
    key = admin.post("/api/key").json()["key"]
    script = make_client(admin_password=PASSWORD, api_enabled=True)
    assert put(script, hostname="evil.lan").status_code == 401
    assert script.get("/api/settings", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_without_a_password_the_settings_are_open(client):
    assert put(client, hostname="links.lan").status_code == 200


# --- Configuration ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("", None), ("true", True), ("ON", True), ("1", True), ("no", False)])
def test_api_enabled_from_the_environment(value, expected):
    assert Settings.from_env({"API_ENABLED": value}).api_enabled is expected


def test_api_enabled_must_be_a_boolean():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"API_ENABLED": "maybe"})
    assert str(info.value) == "API_ENABLED must be true or false, got 'maybe'"

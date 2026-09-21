"""Local only: the interface and the API refuse addresses outside the local network; the links stay public."""

import pytest
from fastapi.testclient import TestClient
from support import create

from redirect.config import Settings
from redirect.main import is_local_address

PUBLIC = ("203.0.113.9", 40000)
DENIED_JSON = {"detail": "Redirect's interface and API only answer on the local network.", "errors": {}}
DENIED_TEXT = "Redirect's interface only answers on the local network. The links it gives out work from anywhere."


def from_address(client, address=PUBLIC) -> TestClient:
    """The same running app, seen from another address."""
    return TestClient(client.app, client=address)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.5.4",
        "192.168.1.20",
        "169.254.3.4",
        "100.101.102.103",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:192.168.1.20",
    ],
)
def test_local_addresses(host):
    assert is_local_address(host)


# 203.0.113.0/24 and 198.18.0.0/15 are documentation and benchmark ranges: "private" for Python, not local
@pytest.mark.parametrize(
    "host", ["203.0.113.9", "198.18.0.1", "8.8.8.8", "2001:4860::8888", "::ffff:8.8.8.8", "testclient", "", None]
)
def test_other_addresses(host):
    assert not is_local_address(host)


@pytest.mark.parametrize(
    "path", ["/api/redirects", "/api/list", "/api/logs", "/api/settings", "/api/auth", "/api/qrcodes"]
)
def test_the_api_refuses_a_public_address(client, path):
    response = from_address(client).get(path)
    assert response.status_code == 403
    assert response.json() == DENIED_JSON
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_interface_refuses_a_public_address_with_a_page(client):
    response = from_address(client).get("/")
    assert response.status_code == 403
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "<title>Not available from here</title>" in response.text
    assert f"<p>{DENIED_TEXT.replace("'", '&#x27;')}</p>" in response.text
    # No link to the interface, which would only lead back here
    assert "Open Redirect" not in response.text


def test_a_login_attempt_from_outside_learns_nothing(make_client):
    client = make_client(admin_password="correct horse battery")
    response = from_address(client).post("/api/login", json={"password": "correct horse battery"})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_the_links_stay_public(client):
    wiki = create(client, "wiki", target="http://w/")
    outside = from_address(client)
    for path in ("/redirect/wiki", f"/links/{wiki['id']}"):
        assert outside.get(path, follow_redirects=False).status_code == 302
    assert outside.get("/redirect/nope", follow_redirects=False).status_code == 404
    assert outside.get("/api/health").status_code == 200
    assert outside.get("/static/app.css").status_code == 200


def test_turned_off_in_the_interface(client):
    response = client.put(
        "/api/settings",
        json={"hostname": "redirect.lan", "hostname_enabled": True, "api_enabled": False, "local_only": False},
    )
    assert response.json()["local_only"] is False
    assert from_address(client).get("/api/redirects").status_code == 200


def test_leaving_the_field_out_keeps_the_setting(client):
    client.put("/api/settings", json={"hostname": "redirect.lan", "hostname_enabled": True, "api_enabled": False})
    assert client.get("/api/settings").json()["local_only"] is True


def test_the_setting_survives_a_restart(make_client, client):
    client.put(
        "/api/settings",
        json={"hostname": "redirect.lan", "hostname_enabled": True, "api_enabled": False, "local_only": False},
    )
    restarted = make_client()
    assert from_address(restarted).get("/api/redirects").status_code == 200


@pytest.mark.parametrize("forced", [True, False])
def test_local_only_in_the_environment_wins(make_client, forced):
    forced_client = make_client(local_only=forced)
    status = forced_client.get("/api/settings").json()
    assert (status["local_only"], status["local_only_forced"]) == (forced, True)
    refused = forced_client.put(
        "/api/settings",
        json={"hostname": "redirect.lan", "hostname_enabled": True, "api_enabled": False, "local_only": not forced},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "LOCAL_ONLY in the container's environment decides whether Redirect only answers on the local network."
    )
    assert (from_address(forced_client).get("/api/redirects").status_code == 403) is forced


@pytest.mark.parametrize(("value", "expected"), [("", None), ("true", True), ("off", False)])
def test_local_only_from_the_environment(value, expected):
    assert Settings.from_env({"LOCAL_ONLY": value}).local_only is expected


def test_local_only_must_be_a_boolean():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"LOCAL_ONLY": "lan"})
    assert str(info.value) == "LOCAL_ONLY must be true or false, got 'lan'"

"""Local only: the interface and the API refuse addresses outside the local network; the links stay public."""

import logging
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from support import FakeAnnouncer, create

from redirect.config import Settings
from redirect.main import create_app, is_local_address
from redirect.server import DroppingH11Protocol

PUBLIC = ("203.0.113.9", 40000)


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
    "path", ["/", "/api/redirects", "/api/list", "/api/logs", "/api/settings", "/api/auth", "/api/qrcodes"]
)
def test_a_public_address_learns_nothing(client, path):
    """Without the server's protocol (here, the test client) a refusal is a bare 403: no page, no JSON."""
    response = from_address(client).get(path)
    assert response.status_code == 403
    assert response.content == b""
    assert "content-type" not in response.headers


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


# --- Under the real server: the connection is reset, as if nothing listened -------------------------------


@pytest.fixture
def served(tmp_path):
    """The app under uvicorn with Redirect's protocol, on a free port. X-Forwarded-For from 127.0.0.1 is trusted
    (uvicorn's default), which lets the test speak from any address."""
    app = create_app(Settings(data_dir=tmp_path), announcer=FakeAnnouncer())
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, http=DroppingH11Protocol, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.05)
    errors: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            errors.append(record.getMessage())

    handler = Collect(level=logging.ERROR)
    logging.getLogger("uvicorn.error").addHandler(handler)
    yield sock.getsockname()[1], errors
    logging.getLogger("uvicorn.error").removeHandler(handler)
    server.should_exit = True
    thread.join(30)
    sock.close()


def exchange(port: int, path: str, address: str | None = None) -> bytes:
    """Sends one request and reads what comes back until the server closes (or resets) the connection."""
    forwarded = f"X-Forwarded-For: {address}\r\n" if address else ""
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{forwarded}Connection: close\r\n\r\n".encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(request)
        received = b""
        try:
            while chunk := connection.recv(4096):
                received += chunk
        except ConnectionResetError:
            return received + b"<reset>"
    return received


@pytest.mark.parametrize("path", ["/", "/api/list", "/api/settings"])
def test_under_the_server_a_public_address_gets_a_reset(served, path):
    port, errors = served
    assert exchange(port, path, "203.0.113.9") == b"<reset>"
    # uvicorn sends nothing after the reset, and logs no error (no 500 behind the scenes)
    assert errors == []


def test_under_the_server_the_links_still_answer_a_public_address(served):
    port, _ = served
    assert exchange(port, "/redirect/nope", "203.0.113.9").startswith(b"HTTP/1.1 404 ")
    assert exchange(port, "/api/health", "203.0.113.9").startswith(b"HTTP/1.1 200 ")


def test_under_the_server_a_local_address_is_answered(served):
    port, _ = served
    assert exchange(port, "/api/list").startswith(b"HTTP/1.1 200 ")
    assert exchange(port, "/api/list", "192.168.1.30").startswith(b"HTTP/1.1 200 ")

"""HEADLESS: no interface; the redirects are managed through the API, with the API key only."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import LOCAL_CLIENT

from redirect.config import Settings

RUNNING = "Redirect is running"
KEY_REQUIRED = {"detail": "Send the API key: Authorization: Bearer <key>.", "errors": {}}
BODY = {"slug": "wiki", "name": "Wiki", "target": "http://wiki.example/"}


@pytest.fixture
def messages() -> Iterator[list[str]]:
    collected: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record.getMessage())

    handler = Collect()
    logging.getLogger("redirect").addHandler(handler)
    yield collected
    logging.getLogger("redirect").removeHandler(handler)


def key_in(messages: list[str]) -> str:
    [line] = [m for m in messages if m.startswith("HEADLESS: API key created")]
    return line.rsplit(" ", 1)[-1]


@pytest.fixture
def headless(make_client, messages):
    """A headless app, and the key it printed at its first start."""
    client = make_client(headless=True, new_key=lambda: "headless-key")
    return client, key_in(messages)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_the_first_start_prints_a_key_once(make_client, headless, messages):
    _, key = headless
    assert key == "rdk_headless-key"
    make_client(headless=True)  # a restart: the key exists, nothing is printed again
    assert len([m for m in messages if m.startswith("HEADLESS: API key created")]) == 1


def plain(response, status: int) -> None:
    """One line of plain text: no page, no style sheet, no JSON."""
    assert response.status_code == status
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == RUNNING


def test_the_interface_is_one_line(headless):
    client, _ = headless
    plain(client.get("/"), 200)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth"),
        ("POST", "/api/login"),
        ("GET", "/api/settings"),
        ("POST", "/api/key"),
        ("POST", "/api/qrcodes/preview"),
        ("GET", "/static/app.css"),
        ("GET", "/static/app.js"),
        ("GET", "/anything"),
    ],
)
def test_everything_of_the_interface_is_the_same_line(headless, method, path):
    client, key = headless
    plain(client.request(method, path, headers=bearer(key), json={}), 404)


def test_a_link_that_leads_nowhere_is_the_same_line(headless):
    client, _ = headless
    plain(client.get("/redirect/nope", follow_redirects=False), 404)
    plain(client.get("/links/Zz9Zz9Zz", follow_redirects=False), 404)


def test_the_health_check_stays(headless):
    client, _ = headless
    assert client.get("/api/health").json() == {"status": "ok"}


def test_the_api_needs_the_key(headless):
    client, key = headless
    response = client.get("/api/redirects")
    assert response.status_code == 401
    assert response.json() == KEY_REQUIRED
    assert client.get("/api/redirects", headers=bearer("rdk_wrong")).status_code == 401
    assert client.get("/api/list", headers=bearer(key)).status_code == 200


def test_the_whole_api_works_with_the_key(headless):
    client, key = headless
    created = client.post("/api/redirects", json=BODY, headers=bearer(key))
    assert created.status_code == 201
    nano = client.post("/api/nanourls", json={"name": "Lunch", "target": "http://l/"}, headers=bearer(key))
    qr = client.post("/api/qrcodes", json={"name": "Menu", "target": "http://m/"}, headers=bearer(key))
    assert (nano.status_code, qr.status_code) == (201, 201)
    assert qr.json()["svg"].startswith("<svg")
    assert client.delete(f"/api/redirects/{created.json()['id']}", headers=bearer(key)).status_code == 204
    changes = client.get("/api/logs", params={"kind": "changes"}, headers=bearer(key)).json()["events"]
    assert [(e["action"], e["source"]) for e in changes] == [
        ("delete", "api"),
        ("create", "api"),
        ("create", "api"),
        ("create", "api"),
    ]


def test_the_links_stay_public(headless):
    client, key = headless
    created = client.post("/api/redirects", json=BODY, headers=bearer(key)).json()
    for path in ("/redirect/wiki", f"/links/{created['id']}"):
        assert client.get(path, follow_redirects=False).status_code == 302


def test_a_password_does_not_open_the_interface(make_client, messages):
    client = make_client(headless=True, admin_password="correct horse battery")
    key = key_in(messages)
    assert client.post("/api/login", json={"password": "correct horse battery"}).status_code == 404
    assert client.get("/api/redirects", headers=bearer(key)).status_code == 200


def test_outside_the_local_network_the_api_still_answers_403(headless):
    client, key = headless
    outside = TestClient(client.app, client=("203.0.113.9", 40000))
    assert outside.get("/api/redirects", headers=bearer(key)).status_code == 403
    # From outside too, anything but the API is the same line
    plain(outside.get("/"), 200)
    plain(outside.get("/redirect"), 404)


def test_the_api_key_command_replaces_the_key_while_running(headless, tmp_path):
    client, old = headless
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run(
        [sys.executable, "-m", "redirect", "api-key"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    new = result.stdout.strip()
    assert new.startswith("rdk_") and new != old
    # The running app has not restarted: it reads the stored key again when it meets an unknown one
    assert client.get("/api/list", headers=bearer(new)).status_code == 200
    assert client.get("/api/list", headers=bearer(old)).status_code == 401


def test_the_command_refuses_other_arguments(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "redirect", "new-key"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (result.returncode, result.stderr) == (1, "usage: python -m redirect [api-key]\n")


@pytest.mark.parametrize(("value", "expected"), [("", False), ("true", True), ("0", False)])
def test_headless_from_the_environment(value, expected):
    assert Settings.from_env({"HEADLESS": value}).headless is expected


def test_headless_needs_the_api():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"HEADLESS": "true", "API_ENABLED": "false"})
    assert str(info.value) == "HEADLESS needs the API: remove API_ENABLED=false"


def test_headless_must_be_a_boolean():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"HEADLESS": "maybe"})
    assert str(info.value) == "HEADLESS must be true or false, got 'maybe'"


def test_the_local_client_constant_is_local():
    from redirect.main import is_local_address

    assert is_local_address(LOCAL_CLIENT[0])

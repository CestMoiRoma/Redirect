"""The log: every visit of a link, every change of a redirect, every read by a script; GET /api/logs; the purge."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from support import create

from redirect.config import Settings

PASSWORD = "correct horse battery"
UA = {"User-Agent": "TestBrowser/1.0"}


def events(client, **params):
    response = client.get("/api/logs", params=params)
    assert response.status_code == 200
    return response.json()


def summary(client, **params):
    return [
        (e["action"], e["source"], e["redirect_id"], e["name"], e["path"], e["status"])
        for e in events(client, **params)["events"]
    ]


def test_a_new_log_is_empty(client):
    assert events(client) == {"events": [], "next": None}


def test_visits_are_logged_with_the_client(client):
    wiki = create(client, "wiki", name="Wiki")
    client.get("/redirect/wiki", headers=UA, follow_redirects=False)
    client.get(f"/links/{wiki['id']}", headers=UA, follow_redirects=False)
    client.get("/redirect/nope", headers=UA, follow_redirects=False)
    [nope, short, path, created] = events(client)["events"]
    assert (path["action"], path["source"], path["path"], path["status"]) == ("visit", "link", "/redirect/wiki", 302)
    assert (short["path"], short["status"]) == (f"/links/{wiki['id']}", 302)
    assert (nope["path"], nope["status"]) == ("/redirect/nope", 404)
    assert path["client"] == "testclient"
    assert path["user_agent"] == "TestBrowser/1.0"
    assert path["at"] == "2026-01-15T09:30:00Z"
    assert created["action"] == "create"


def test_changes_are_logged_with_the_redirect_as_it_was(client):
    wiki = create(client, "wiki", name="Wiki")
    client.put(f"/api/redirects/{wiki['id']}", json={"slug": "team/wiki", "name": "Team wiki", "target": "http://w/"})
    client.delete(f"/api/redirects/{wiki['id']}")
    assert summary(client) == [
        ("delete", "interface", wiki["id"], "Team wiki", "team/wiki", None),
        ("update", "interface", wiki["id"], "Team wiki", "team/wiki", None),
        ("create", "interface", wiki["id"], "Wiki", "wiki", None),
    ]


def test_refused_changes_are_not_logged(client):
    create(client, "wiki")
    client.post("/api/redirects", json={"slug": "wiki", "name": "Again", "target": "http://w/"})
    client.post("/api/redirects", json={"slug": "a b", "name": "Bad", "target": "http://w/"})
    client.delete("/api/redirects/nothing")
    assert [e[0] for e in summary(client)] == ["create"]


def test_the_interface_reading_the_list_is_not_logged(client):
    client.get("/api/redirects")
    client.get("/api/list")
    assert summary(client) == []


def test_scripts_are_logged_as_api(make_client):
    admin = make_client(admin_password=PASSWORD, api_enabled=True)
    admin.post("/api/login", json={"password": PASSWORD})
    key = admin.post("/api/key").json()["key"]
    script = TestClient(admin.app, headers={"Authorization": f"Bearer {key}"})
    created = script.post("/api/redirects", json={"slug": "wiki", "name": "Wiki", "target": "http://w/"}).json()
    script.get("/api/list")
    script.get("/api/logs", params={"limit": 1})
    assert summary(admin) == [
        ("read", "api", None, None, "/api/logs", None),
        ("read", "api", None, None, "/api/list", None),
        ("create", "api", created["id"], "Wiki", "wiki", None),
    ]


@pytest.mark.parametrize(
    ("kind", "actions"),
    [("visits", ["visit", "visit"]), ("changes", ["update", "create"]), ("reads", [])],
)
def test_the_log_can_be_filtered(client, kind, actions):
    wiki = create(client, "wiki")
    client.put(f"/api/redirects/{wiki['id']}", json={"slug": "wiki", "name": "W", "target": "http://w/"})
    client.get("/redirect/wiki", follow_redirects=False)
    client.get("/redirect/nope", follow_redirects=False)
    assert [e["action"] for e in events(client, kind=kind)["events"]] == actions


def test_the_log_is_paged_newest_first(client):
    for number in range(5):
        client.get(f"/redirect/page-{number}", follow_redirects=False)
    first = events(client, limit=2)
    assert [e["path"] for e in first["events"]] == ["/redirect/page-4", "/redirect/page-3"]
    second = events(client, limit=2, before=first["next"])
    assert [e["path"] for e in second["events"]] == ["/redirect/page-2", "/redirect/page-1"]
    third = events(client, limit=2, before=second["next"])
    assert [e["path"] for e in third["events"]] == ["/redirect/page-0"]
    assert third["next"] is None


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"before": 0}, {"kind": "everything"}])
def test_bad_log_queries_are_refused(client, params):
    assert client.get("/api/logs", params=params).status_code == 422


def test_long_user_agents_and_paths_are_cut(client):
    client.get("/redirect/" + "a" * 400, headers={"User-Agent": "x" * 500}, follow_redirects=False)
    [event] = events(client)["events"]
    assert len(event["user_agent"]) == 300
    assert len(event["path"]) == 300


def test_the_log_is_purged_after_the_retention(make_client, clock):
    client = make_client()
    client.get("/redirect/old", follow_redirects=False)
    clock.advance(days=90, seconds=1)
    restarted = make_client()  # the purge runs at startup, then at most hourly
    restarted.get("/redirect/new", follow_redirects=False)
    assert [e["path"] for e in events(restarted)["events"]] == ["/redirect/new"]


def test_the_retention_is_set_by_the_environment():
    assert Settings.from_env({}).log_retention_days == 90
    assert Settings.from_env({"LOG_RETENTION_DAYS": "7"}).log_retention_days == 7
    for bad in ("0", "-3", "two weeks"):
        with pytest.raises(ValueError) as info:
            Settings.from_env({"LOG_RETENTION_DAYS": bad})
        assert str(info.value) == f"LOG_RETENTION_DAYS must be a whole number of days, 1 or more, got {bad!r}"


def test_the_log_needs_the_session_or_the_key(make_client):
    client = make_client(admin_password=PASSWORD)
    assert client.get("/api/logs").status_code == 401


def test_a_broken_log_never_breaks_a_redirect(client, tmp_path):
    create(client, "wiki", target="http://w/")
    with sqlite3.connect(tmp_path / "data" / "redirect.db") as conn:
        conn.execute("DROP TABLE events")
    response = client.get("/redirect/wiki", follow_redirects=False)
    assert response.status_code == 302

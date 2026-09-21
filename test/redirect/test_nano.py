"""NanoURL: a name and a target make a short link, /links/<id>, filed under NanoURL/<name>."""

import pytest
from support import create

from redirect.models import slug_from_name

TARGET = "https://intranet.example/very/long/path?with=query&and=more"
NAME_TAKEN = {
    "detail": "This name is already used in this section.",
    "errors": {"name": "This name is already used in this section."},
}


def nano(client, name="Team lunch", target=TARGET):
    return client.post("/api/nanourls", json={"name": name, "target": target})


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Team lunch", "NanoURL/Team-lunch"),
        ("Budget 2026 !", "NanoURL/Budget-2026"),
        ("Réunion d'équipe", "NanoURL/Reunion-d-equipe"),
        ("  a  --  b  ", "NanoURL/a-b"),
        ("v1.2_final", "NanoURL/v1.2_final"),
        ("...dots...", "NanoURL/dots"),
    ],
)
def test_the_path_comes_from_the_name(name, slug):
    assert slug_from_name("NanoURL", name) == slug


@pytest.fixture
def lan(make_client):
    """A client that reaches Redirect on its LAN address, as a browser of the network does."""
    return make_client(base_url="http://192.168.1.20")


def test_creating_answers_201_with_the_id_and_the_short_link(lan):
    response = nano(lan)
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "nano"
    assert body["id"] == "id000001"
    # The IP address by default: the link works without any DNS
    assert body["link_host"] == "ip"
    assert body["short_url"] == "http://192.168.1.20/links/id000001"
    assert body["slug"] == "NanoURL/Team-lunch"
    assert body["public_url"] == "http://192.168.1.20/redirect/NanoURL/Team-lunch"
    assert body["name"] == "Team lunch"


def test_the_hostname_is_an_option(lan):
    response = lan.post("/api/nanourls", json={"name": "Lunch", "target": TARGET, "link_host": "hostname"})
    assert response.json()["link_host"] == "hostname"
    assert response.json()["short_url"] == "http://redirect.lan/links/id000001"


def test_the_option_can_change_later(lan):
    body = nano(lan).json()
    changed = lan.put(
        f"/api/nanourls/{body['id']}", json={"name": "Team lunch", "target": TARGET, "link_host": "hostname"}
    ).json()
    assert changed["short_url"] == f"http://redirect.lan/links/{body['id']}"
    # Both links lead to the same place: only the one shown changes
    assert lan.get(f"/links/{body['id']}", follow_redirects=False).status_code == 302


def test_an_unknown_option_is_refused(lan):
    response = lan.post("/api/nanourls", json={"name": "Lunch", "target": TARGET, "link_host": "dns"})
    assert response.status_code == 422
    assert response.json()["errors"] == {"link_host": "This value is not valid."}


def test_the_ip_link_carries_the_port_when_it_is_not_80(make_client):
    client = make_client(port=8081, base_url="http://192.168.1.20:8081")
    assert nano(client).json()["short_url"] == "http://192.168.1.20:8081/links/id000001"


def test_public_base_url_wins_over_the_ip(make_client):
    client = make_client(public_base_url="https://go.example.org", base_url="http://192.168.1.20")
    assert nano(client).json()["short_url"] == "https://go.example.org/links/id000001"


@pytest.mark.parametrize(
    ("scheme", "host", "port", "base"),
    [
        ("http", "192.168.1.20", 80, "http://192.168.1.20"),
        ("http", "192.168.1.20", 8080, "http://192.168.1.20:8080"),
        ("https", "192.168.1.20", 443, "https://192.168.1.20"),
        ("http", "fd00::20", 80, "http://[fd00::20]"),
    ],
)
def test_ip_base_url(scheme, host, port, base):
    from redirect.main import ip_base_url

    assert ip_base_url(scheme, host, port) == base


def test_the_lists_give_both_bases(lan):
    listed = lan.get("/api/nanourls").json()
    assert (listed["base_url"], listed["ip_base_url"]) == ("http://redirect.lan", "http://192.168.1.20")


def test_both_links_lead_to_the_target(client):
    body = nano(client).json()
    for path in (f"/links/{body['id']}", "/redirect/NanoURL/Team-lunch"):
        response = client.get(path, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (302, TARGET)


def test_nanos_have_their_own_list_and_are_in_the_full_one(client):
    create(client, "wiki")
    body = nano(client).json()
    assert [r["id"] for r in client.get("/api/nanourls").json()["redirects"]] == [body["id"]]
    assert sorted(r["kind"] for r in client.get("/api/list").json()["redirects"]) == ["link", "nano"]


def test_a_name_already_used_is_refused(client):
    nano(client)
    response = nano(client, name="Team  lunch")  # the same path once made from the name
    assert response.status_code == 409
    assert response.json() == NAME_TAKEN


def test_a_name_without_letters_is_refused(client):
    response = nano(client, name="!!!")
    assert response.status_code == 422
    assert response.json() == {
        "detail": "Some fields are invalid.",
        "errors": {"name": "Use at least one letter or digit in the name: the path is made from it."},
    }


def test_the_target_is_validated(client):
    response = nano(client, target="javascript:alert(1)")
    assert response.status_code == 422
    assert response.json()["errors"] == {"target": "Enter a full URL that starts with http:// or https://."}


def test_changing_the_name_moves_the_path_but_keeps_the_short_link(client):
    body = nano(client).json()
    response = client.put(
        f"/api/nanourls/{body['id']}", json={"name": "Friday lunch", "target": "https://new.example/"}
    )
    assert response.status_code == 200
    changed = response.json()
    assert (changed["id"], changed["slug"], changed["short_url"]) == (
        body["id"],
        "NanoURL/Friday-lunch",
        body["short_url"],
    )
    assert client.get(f"/links/{body['id']}", follow_redirects=False).headers["location"] == "https://new.example/"


def test_deleting(client):
    body = nano(client).json()
    assert client.delete(f"/api/nanourls/{body['id']}").status_code == 204
    assert client.get(f"/links/{body['id']}", follow_redirects=False).status_code == 404


def test_the_nano_routes_only_touch_nanos(client):
    link = create(client, "wiki")
    assert client.put(f"/api/nanourls/{link['id']}", json={"name": "x", "target": TARGET}).status_code == 404
    assert client.delete(f"/api/nanourls/{link['id']}").status_code == 404


def test_the_generic_route_does_not_change_a_nano(client):
    body = nano(client).json()
    response = client.put(f"/api/redirects/{body['id']}", json={"slug": "lunch", "name": "x", "target": TARGET})
    assert response.status_code == 409
    assert response.json()["detail"] == "Change a NanoURL through /api/nanourls, and a QR code through /api/qrcodes."
    # ...but deletes it like any redirect
    assert client.delete(f"/api/redirects/{body['id']}").status_code == 204


def test_nano_changes_are_logged(client):
    body = nano(client).json()
    client.put(f"/api/nanourls/{body['id']}", json={"name": "Friday lunch", "target": TARGET})
    client.delete(f"/api/nanourls/{body['id']}")
    events = client.get("/api/logs").json()["events"]
    assert [(e["action"], e["path"]) for e in events] == [
        ("delete", "NanoURL/Friday-lunch"),
        ("update", "NanoURL/Friday-lunch"),
        ("create", "NanoURL/Team-lunch"),
    ]


def test_the_key_opens_the_nano_routes(make_client):
    admin = make_client(admin_password="correct horse battery", api_enabled=True)
    admin.post("/api/login", json={"password": "correct horse battery"})
    key = admin.post("/api/key").json()["key"]
    admin.cookies.clear()
    assert nano(admin).status_code == 401
    admin.headers["Authorization"] = f"Bearer {key}"
    assert nano(admin).status_code == 201

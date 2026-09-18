"""The public link: GET /redirect/{slug} answers 302 to the target, or the 404 page. Never 500."""

import asyncio
from urllib.parse import unquote

import pytest
from support import create

NOT_FOUND_TITLE = "<title>Link not found</title>"


UNKNOWN_TEXT = "<p>No redirect exists at this address. The link may have been changed or removed.</p>"


def not_found_text(path: str) -> str:
    return f"<p>No redirect exists for {path}. The link may have been changed or removed.</p>"


class RawResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.text = body.decode("utf-8")


def get_raw(client, raw_path: bytes, method: str = "GET") -> RawResponse:
    """Call the app with the path exactly as a client sent it (as `curl --path-as-is` does).

    httpx, like browsers, removes dot segments before sending, so this goes to the ASGI app directly,
    with the scope uvicorn builds: `path` percent-decoded, `raw_path` untouched.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw_path.decode("ascii")),
        "raw_path": raw_path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(client.app(scope, receive, send))
    start = messages[0]
    headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return RawResponse(start["status"], headers, body)


@pytest.fixture
def budget(client):
    return create(client, "compta/budget-2026", target="http://files.intranet.local/compta/Budget%202026.xlsx")


def test_known_slug_redirects_with_302(client, budget):
    response = client.get("/redirect/compta/budget-2026", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://files.intranet.local/compta/Budget%202026.xlsx"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""


def test_head_redirects_too(client, budget):
    response = client.head("/redirect/compta/budget-2026", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://files.intranet.local/compta/Budget%202026.xlsx"


def test_a_new_target_takes_effect_on_the_next_request(client, budget):
    client.put(
        f"/api/redirects/{budget['id']}",
        json={"slug": budget["slug"], "name": budget["name"], "target": "https://sharepoint.example/budget-v2.xlsx"},
    )
    response = client.get("/redirect/compta/budget-2026", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://sharepoint.example/budget-v2.xlsx"


def test_a_query_string_is_not_part_of_the_slug(client, budget):
    response = client.get("/redirect/compta/budget-2026?utm=mail", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://files.intranet.local/compta/Budget%202026.xlsx"


def test_non_ascii_target_is_sent_percent_encoded(client):
    create(client, "livres", target="https://bücher.example/été")
    response = client.get("/redirect/livres", follow_redirects=False)
    assert response.headers["location"] == "https://xn--bcher-kva.example/%C3%A9t%C3%A9"


def test_unknown_slug_is_the_404_page(client, budget):
    response = client.get("/redirect/compta/budget-2025", follow_redirects=False)
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert NOT_FOUND_TITLE in response.text
    assert "<h1>Link not found</h1>" in response.text
    assert not_found_text("/redirect/compta/budget-2025") in response.text
    assert '<a class="button primary" href="/">Open Redirect</a>' in response.text


def test_slugs_are_case_sensitive(client, budget):
    assert client.get("/redirect/Compta/Budget-2026", follow_redirects=False).status_code == 404


def test_deleted_slug_is_404(client, budget):
    client.delete(f"/api/redirects/{budget['id']}")
    assert client.get("/redirect/compta/budget-2026", follow_redirects=False).status_code == 404


@pytest.mark.parametrize(
    "raw_path",
    [
        b"/redirect/compta/../compta/budget-2026",
        b"/redirect/../redirect/compta/budget-2026",
        b"/redirect/compta/./budget-2026",
        b"/redirect/..",
        b"/redirect/compta/%2e%2e/compta/budget-2026",
        b"/redirect/compta//budget-2026",
        b"/redirect//compta/budget-2026",
        b"/redirect/compta/budget-2026/",
        b"/redirect/",
        b"/redirect",
        b"/redirect/compta%2Fbudget-2026%00",
        b"/redirect/%ff%fe",
        b"/redirect/a%20b",
        # A final newline: the route pattern would match it as "compta/budget-2026", an alias of the real link
        b"/redirect/compta/budget-2026%0A",
        # A newline inside: no route matches at all, and it must still be the HTML page, not a JSON 404
        b"/redirect/a%0Ab",
        b"/redirect/%0A",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_malformed_slugs_are_404_never_500(client, budget, raw_path, method):
    response = get_raw(client, raw_path, method)
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "location" not in response.headers
    if method == "GET":
        assert NOT_FOUND_TITLE in response.text
        # An invalid path is never echoed back
        assert UNKNOWN_TEXT in response.text


def test_the_404_page_never_echoes_an_invalid_path(client):
    response = get_raw(client, b"/redirect/%3Cscript%3Ealert(1)%3C/script%3E")
    assert response.status_code == 404
    assert "<script>" not in response.text
    assert "alert" not in response.text
    assert UNKNOWN_TEXT in response.text


def test_the_404_page_cannot_be_used_to_show_arbitrary_text(client):
    path = "/redirect/x.%20Your%20session%20expired,%20sign%20in%20again%20at%20https:%2F%2Fevil.example%2Flogin"
    response = get_raw(client, path.encode())
    assert response.status_code == 404
    assert "session" not in response.text
    assert "evil.example" not in response.text
    assert UNKNOWN_TEXT in response.text


def test_the_404_page_does_not_echo_a_path_longer_than_a_slug(client):
    response = client.get("/redirect/" + "a" * 256)
    assert response.status_code == 404
    assert UNKNOWN_TEXT in response.text


def test_the_404_page_does_not_substitute_braces_from_the_path(client):
    response = get_raw(client, b"/redirect/%7Bpath%7D%7B%7Btitle%7D%7D")
    assert response.status_code == 404
    assert "{{title}}" not in response.text
    assert UNKNOWN_TEXT in response.text


# --- The database cannot be reached ---------------------------------------------------------------------

UNAVAILABLE_TITLE = "<title>Redirect is unavailable</title>"
UNAVAILABLE_TEXT = "<p>The database cannot be reached right now. Try the link again in a moment.</p>"


def test_a_locked_database_answers_503_on_a_public_link(client, budget, locked_database):
    response = client.get("/redirect/compta/budget-2026", follow_redirects=False)
    assert response.status_code == 503
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert UNAVAILABLE_TITLE in response.text
    assert UNAVAILABLE_TEXT in response.text
    assert '<a class="button primary" href="/">Open Redirect</a>' in response.text


def test_a_missing_database_answers_503_and_is_not_recreated(client, budget, tmp_path):
    database = tmp_path / "data" / "redirect.db"
    database.unlink()
    response = client.get("/redirect/compta/budget-2026", follow_redirects=False)
    assert response.status_code == 503
    assert UNAVAILABLE_TEXT in response.text
    assert not database.exists()

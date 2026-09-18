"""API tests on the real app (TestClient, temporary SQLite database): every route, every error, verbatim."""

import pytest
from support import PUBLIC_BASE_URL, create

FIELDS_INVALID = "Some fields are invalid."
SLUG_TAKEN = "This path is already used by another redirect."
NOT_FOUND = "This redirect no longer exists."
BUDGET = {
    "slug": "compta/budget-2026",
    "name": "Budget 2026",
    "target": "http://files.intranet.local/compta/Budget%202026.xlsx",
}


def invalid(errors: dict[str, str]) -> dict:
    return {"detail": FIELDS_INVALID, "errors": errors}


# --- Health, front end, headers ------------------------------------------------------------------------


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_an_unreachable_database(client, tmp_path):
    database = tmp_path / "data" / "redirect.db"
    database.unlink()
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    # At run time a missing database is reported, never replaced by a new empty one
    assert not database.exists()


DATABASE_UNAVAILABLE = {"detail": "The database cannot be reached right now. Try again in a moment.", "errors": {}}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/redirects", None),
        ("POST", "/api/redirects", BUDGET),
        ("PUT", "/api/redirects/1", BUDGET),
        ("DELETE", "/api/redirects/1", None),
    ],
)
def test_a_locked_database_answers_503_on_the_api(client, locked_database, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.json() == DATABASE_UNAVAILABLE
    assert response.headers["retry-after"] == "5"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_missing_database_answers_503_on_the_api(client, tmp_path):
    (tmp_path / "data" / "redirect.db").unlink()
    response = client.get("/api/redirects")
    assert response.status_code == 503
    assert response.json() == DATABASE_UNAVAILABLE


def test_index_serves_the_single_page_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert '<script src="/static/app.js" defer></script>' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.js", "text/javascript; charset=utf-8"),
        ("/static/app.css", "text/css; charset=utf-8"),
        ("/static/strings.json", "application/json"),
        ("/static/favicon.svg", "image/svg+xml"),
        ("/static/fonts/inter-latin-400-normal.woff2", "font/woff2"),
        ("/static/fonts/jetbrains-mono-latin-600-normal.woff2", "font/woff2"),
    ],
)
def test_static_files_are_served(client, path, content_type):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    # Revalidated on every load: after an upgrade, index.html never runs with an older app.js or strings.json
    assert response.headers["cache-control"] == "no-cache"


def test_unknown_static_file_is_404(client):
    assert client.get("/static/missing.js").status_code == 404


@pytest.mark.parametrize("path", ["/", "/api/redirects", "/api/health", "/redirect/nope", "/static/app.css"])
def test_security_headers_on_every_response(client, path):
    headers = client.get(path).headers
    assert headers["content-security-policy"] == (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self';"
        " base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_interactive_docs_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/api/openapi.json").status_code == 200


# --- List ----------------------------------------------------------------------------------------------


def test_list_is_empty_on_a_new_database(client):
    response = client.get("/api/redirects")
    assert response.status_code == 200
    assert response.json() == {"base_url": "http://testserver", "redirects": []}


def test_list_uses_the_host_header_without_public_base_url(client):
    create(client, "wiki")
    body = client.get("/api/redirects", headers={"Host": "10.0.0.5:8080"}).json()
    assert body["base_url"] == "http://10.0.0.5:8080"
    assert body["redirects"][0]["public_url"] == "http://10.0.0.5:8080/redirect/wiki"


def test_list_uses_public_base_url_whatever_the_host(public_client):
    create(public_client, "wiki")
    body = public_client.get("/api/redirects", headers={"Host": "10.0.0.5:8080"}).json()
    assert body["base_url"] == PUBLIC_BASE_URL
    assert body["redirects"][0]["public_url"] == "http://redirect.local:8080/redirect/wiki"


def test_list_is_sorted_by_name_case_insensitive_then_slug(client):
    create(client, "c", name="beta")
    create(client, "b", name="Alpha")
    create(client, "a", name="beta")
    create(client, "d", name="alpha zulu")
    body = client.get("/api/redirects").json()
    assert [(r["name"], r["slug"]) for r in body["redirects"]] == [
        ("Alpha", "b"),
        ("alpha zulu", "d"),
        ("beta", "a"),
        ("beta", "c"),
    ]


# --- Create --------------------------------------------------------------------------------------------


def test_create_returns_the_stored_redirect(public_client):
    response = public_client.post("/api/redirects", json=BUDGET)
    assert response.status_code == 201
    assert response.json() == {
        "id": 1,
        **BUDGET,
        "public_url": "http://redirect.local:8080/redirect/compta/budget-2026",
        "created_at": "2026-01-15T09:30:00Z",
        "updated_at": "2026-01-15T09:30:00Z",
    }
    assert public_client.get("/api/redirects").json()["redirects"] == [response.json()]


def test_create_stores_normalized_values(client):
    body = create(client, " wiki ", name="  Team wiki ", target=" https://bücher.example/ä ")
    assert (body["slug"], body["name"], body["target"]) == ("wiki", "Team wiki", "https://xn--bcher-kva.example/%C3%A4")


def test_create_rejects_a_taken_slug(client):
    create(client, "compta/budget-2026")
    response = client.post("/api/redirects", json=BUDGET)
    assert response.status_code == 409
    assert response.json() == {"detail": SLUG_TAKEN, "errors": {"slug": SLUG_TAKEN}}


def test_create_rejects_a_slug_taken_after_normalization(client):
    create(client, "wiki")
    response = client.post("/api/redirects", json={**BUDGET, "slug": "  wiki  "})
    assert response.status_code == 409


def test_slugs_differing_only_by_case_are_distinct(client):
    create(client, "local-api/Test1")
    create(client, "local-api/test1")
    assert len(client.get("/api/redirects").json()["redirects"]) == 2


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        (
            {"slug": "compta/budget 2026"},
            {"slug": "Use only letters, digits, dots, dashes and underscores, with / between segments."},
        ),
        ({"slug": "compta//budget"}, {"slug": "The path cannot start or end with /, or contain //."}),
        ({"slug": "compta/../budget"}, {"slug": "A path segment cannot be . or .."}),
        ({"slug": "  "}, {"slug": "Enter a path."}),
        ({"slug": "s" * 256}, {"slug": "The path must be at most 255 characters."}),
        ({"name": ""}, {"name": "Enter a name."}),
        ({"name": "n" * 101}, {"name": "The name must be at most 100 characters."}),
        ({"name": "tab\tname"}, {"name": "The name cannot contain control characters."}),
        (
            {"target": "files.intranet.local/budget.xlsx"},
            {"target": "Enter a full URL that starts with http:// or https://."},
        ),
        ({"target": "javascript:alert(1)"}, {"target": "Enter a full URL that starts with http:// or https://."}),
        ({"target": ""}, {"target": "Enter a target URL."}),
        (
            {"target": "https://example.com/" + "x" * 2029},
            {"target": "The target URL must be at most 2048 characters."},
        ),
        (
            {"slug": "a b", "name": " ", "target": "ftp://x"},
            {
                "slug": "Use only letters, digits, dots, dashes and underscores, with / between segments.",
                "name": "Enter a name.",
                "target": "Enter a full URL that starts with http:// or https://.",
            },
        ),
    ],
)
def test_create_validation_errors(client, changes, errors):
    response = client.post("/api/redirects", json={**BUDGET, **changes})
    assert response.status_code == 422
    assert response.json() == invalid(errors)
    assert client.get("/api/redirects").json()["redirects"] == []


def test_create_with_missing_fields(client):
    response = client.post("/api/redirects", json={})
    assert response.status_code == 422
    assert response.json() == invalid(
        {"slug": "Enter a path.", "name": "Enter a name.", "target": "Enter a target URL."}
    )


def test_create_with_values_of_the_wrong_type(client):
    response = client.post("/api/redirects", json={"slug": 12, "name": None, "target": ["http://x"]})
    assert response.status_code == 422
    assert response.json() == invalid(
        {"slug": "This value is not valid.", "name": "This value is not valid.", "target": "This value is not valid."}
    )


def test_create_ignores_unknown_fields(client):
    response = client.post("/api/redirects", json={**BUDGET, "id": 99, "created_at": "yesterday"})
    assert response.status_code == 201
    assert response.json()["id"] == 1
    assert response.json()["created_at"] == "2026-01-15T09:30:00Z"


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (b"{not json", {"Content-Type": "application/json"}),
        (b"", {"Content-Type": "application/json"}),
        (b'["a", "b"]', {"Content-Type": "application/json"}),
        (b"slug=a&name=b", {"Content-Type": "application/x-www-form-urlencoded"}),
    ],
)
def test_create_with_a_body_that_is_not_a_json_object(client, content, headers):
    response = client.post("/api/redirects", content=content, headers=headers)
    assert response.status_code == 422
    assert response.json() == {"detail": "The request body is not valid JSON.", "errors": {}}


# --- Update --------------------------------------------------------------------------------------------


def test_update_changes_every_field_and_the_update_time(public_client, clock):
    created = create(public_client, "compta/budget-2026", name="Budget", target="http://old.example/")
    clock.advance(hours=2)
    response = public_client.put(
        f"/api/redirects/{created['id']}",
        json={"slug": "compta/2026/budget", "name": "Budget 2026", "target": "http://new.example/"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": created["id"],
        "slug": "compta/2026/budget",
        "name": "Budget 2026",
        "target": "http://new.example/",
        "public_url": "http://redirect.local:8080/redirect/compta/2026/budget",
        "created_at": "2026-01-15T09:30:00Z",
        "updated_at": "2026-01-15T11:30:00Z",
    }
    assert public_client.get("/api/redirects").json()["redirects"] == [response.json()]


def test_update_can_keep_its_own_slug(client):
    created = create(client, "wiki")
    response = client.put(
        f"/api/redirects/{created['id']}", json={"slug": "wiki", "name": "Wiki", "target": "http://w/"}
    )
    assert response.status_code == 200


def test_update_rejects_the_slug_of_another_redirect(client):
    create(client, "wiki")
    other = create(client, "docs")
    response = client.put(f"/api/redirects/{other['id']}", json={"slug": "wiki", "name": "Docs", "target": "http://d/"})
    assert response.status_code == 409
    assert response.json() == {"detail": SLUG_TAKEN, "errors": {"slug": SLUG_TAKEN}}
    assert client.get("/api/redirects").json()["redirects"][0]["slug"] == "docs"


def test_update_of_an_unknown_id_is_404(client):
    response = client.put("/api/redirects/42", json=BUDGET)
    assert response.status_code == 404
    assert response.json() == {"detail": NOT_FOUND, "errors": {}}


def test_update_validates_before_looking_up_the_id(client):
    response = client.put("/api/redirects/42", json={**BUDGET, "target": "nope"})
    assert response.status_code == 422
    assert response.json() == invalid({"target": "Enter a full URL that starts with http:// or https://."})


def test_update_validation_error_leaves_the_redirect_unchanged(client):
    created = create(client, "wiki", target="http://w/")
    response = client.put(
        f"/api/redirects/{created['id']}", json={"slug": "wiki/", "name": "Wiki", "target": "http://x/"}
    )
    assert response.status_code == 422
    assert response.json() == invalid({"slug": "The path cannot start or end with /, or contain //."})
    assert client.get("/api/redirects").json()["redirects"] == [created]


@pytest.mark.parametrize("redirect_id", ["abc", "1.5"])
def test_update_with_a_non_integer_id(client, redirect_id):
    response = client.put(f"/api/redirects/{redirect_id}", json=BUDGET)
    assert response.status_code == 422
    assert response.json() == invalid({})


# --- Delete --------------------------------------------------------------------------------------------


def test_delete_removes_the_redirect(client):
    kept = create(client, "wiki")
    gone = create(client, "docs")
    response = client.delete(f"/api/redirects/{gone['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/redirects").json()["redirects"] == [kept]


def test_delete_of_an_unknown_id_is_404(client):
    created = create(client, "wiki")
    assert client.delete(f"/api/redirects/{created['id']}").status_code == 204
    response = client.delete(f"/api/redirects/{created['id']}")
    assert response.status_code == 404
    assert response.json() == {"detail": NOT_FOUND, "errors": {}}


def test_a_deleted_slug_can_be_reused(client):
    created = create(client, "wiki")
    client.delete(f"/api/redirects/{created['id']}")
    assert create(client, "wiki")["id"] == created["id"] + 1


@pytest.mark.parametrize("redirect_id", ["0", "-1", str(2**63), "99999999999999999999"])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_an_id_outside_the_sqlite_range_is_404_not_500(client, method, redirect_id):
    create(client, "wiki")
    response = client.request(method, f"/api/redirects/{redirect_id}", json=BUDGET if method == "PUT" else None)
    assert response.status_code == 404
    assert response.json() == {"detail": NOT_FOUND, "errors": {}}


def test_delete_with_a_non_integer_id(client):
    response = client.delete("/api/redirects/abc")
    assert response.status_code == 422
    assert response.json() == invalid({})


# --- Methods -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("DELETE", "/api/redirects"), ("PUT", "/api/redirects"), ("POST", "/api/redirects/1"), ("POST", "/redirect/wiki")],
)
def test_wrong_method_is_405(client, method, path):
    assert client.request(method, path).status_code == 405


@pytest.mark.parametrize("path", ["/api/nope", "/nope", "/redirects"])
def test_unknown_paths_outside_redirect_keep_the_json_404(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

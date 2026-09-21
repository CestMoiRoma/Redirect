"""QR codes: the drawing is faithful to the matrix, the design is validated, the API serves the SVG."""

import base64
import re

import pytest
import segno
from pydantic import ValidationError

from redirect.models import QRDesign
from redirect.qrcode import EYE, layout, render_svg

URL = "http://redirect.lan/links/Kb7mQx2T"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()
TARGET = "https://menu.example/today"


def finder_cells(x0: int, y0: int) -> set[tuple[int, int]]:
    """The dark modules of a standard finder pattern: the 7x7 ring and the 3x3 center."""
    cells = set()
    for y in range(7):
        for x in range(7):
            ring = x in (0, 6) or y in (0, 6)
            center = 2 <= x <= 4 and 2 <= y <= 4
            if ring or center:
                cells.add((x0 + x, y0 + y))
    return cells


@pytest.mark.parametrize("logo", [None, PNG])
@pytest.mark.parametrize("url", [URL, "https://go.example.org/links/Zz9Zz9Zz"])
def test_the_drawing_is_the_matrix(url, logo):
    design = QRDesign(logo=logo)
    plan = layout(url, design)
    matrix = segno.make(url, error="h" if logo else "m", micro=False, boost_error=False).matrix
    dark = {(x, y) for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell}
    eyes = set().union(*(finder_cells(x, y) for x, y in plan.eyes))
    # The matrix's finder patterns are the standard ones Redirect draws
    for x0, y0 in plan.eyes:
        area = {(x, y) for x in range(x0, x0 + EYE) for y in range(y0, y0 + EYE)}
        assert dark & area == finder_cells(x0, y0)
    cleared = set()
    if plan.logo_area:
        x0, y0, side = plan.logo_area
        cleared = {(x, y) for x in range(x0, x0 + side) for y in range(y0, y0 + side)}
    assert set(plan.modules) | eyes == dark - cleared


def test_a_logo_raises_the_error_correction_and_is_centered():
    plain = layout(URL, QRDesign())
    with_logo = layout(URL, QRDesign(logo=PNG, logo_size=0.3))
    assert with_logo.size >= plain.size  # level H needs more room
    x, y, side = with_logo.logo_area
    assert x == y == (with_logo.size - side) // 2
    # A logo never covers more than 9 % of the code: level H recovers 30 %
    assert side * side <= 0.3 * 0.3 * with_logo.size**2 + 2 * with_logo.size


def test_the_svg_is_stable_and_complete():
    design = QRDesign(color="#0b3d91", eye_color="#c2185b", background="#fffbe6", logo=PNG, margin=2)
    svg = render_svg(URL, design)
    assert svg == render_svg(URL, design)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ')
    assert 'fill="#0b3d91"' in svg and 'fill="#c2185b"' in svg and 'fill="#fffbe6"' in svg
    assert f'href="{PNG}"' in svg
    size = layout(URL, design).size + 4
    assert f'viewBox="0 0 {size} {size}" width="{size * 10}" height="{size * 10}"' in svg


@pytest.mark.parametrize(
    ("style", "element"), [("square", '<path d="M'), ("rounded", "<rect x="), ("dots", "<circle ")]
)
def test_module_styles(style, element):
    assert element in render_svg(URL, QRDesign(module_style=style))


@pytest.mark.parametrize("style", ["square", "rounded", "circle"])
def test_eye_styles_draw_three_eyes(style):
    svg = render_svg(URL, QRDesign(eye_style=style))
    eyes = re.search(r'fill-rule="evenodd" d="([^"]+)"', svg).group(1)
    assert eyes.count("M") == 9  # 3 eyes x (ring outside, ring inside, center)


def test_a_transparent_background_draws_no_background():
    svg = render_svg(URL, QRDesign(background="transparent"))
    assert "<rect width=" not in svg


# --- Design validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"color": "red"}, "Use a color such as #1b1f24."),
        ({"background": "#fff"}, "Use a color such as #1b1f24."),
        ({"color": "#eeeeee"}, "The colors are too close to the background: scanners could not read the QR code."),
        ({"eye_color": "#fafafa"}, "The colors are too close to the background: scanners could not read the QR code."),
        ({"logo": "data:text/html;base64,PHNjcmlwdD4="}, "The logo must be a PNG, JPEG, WebP or SVG image."),
        ({"logo": "data:image/png;base64,not base64!"}, "The logo must be a PNG, JPEG, WebP or SVG image."),
        ({"logo": "https://example.com/logo.png"}, "The logo must be a PNG, JPEG, WebP or SVG image."),
        (
            {"logo": "data:image/png;base64," + base64.b64encode(b"x" * (256 * 1024 + 1)).decode()},
            "The logo must weigh 256 KB or less.",
        ),
    ],
)
def test_bad_designs_are_refused(fields, message):
    with pytest.raises(ValidationError) as info:
        QRDesign(**fields)
    assert info.value.errors()[0]["msg"] == message


@pytest.mark.parametrize(
    "fields", [{"logo_size": 0.05}, {"logo_size": 0.5}, {"margin": -1}, {"margin": 11}, {"module_style": "hearts"}]
)
def test_out_of_range_designs_are_refused(fields):
    with pytest.raises(ValidationError):
        QRDesign(**fields)


def test_a_transparent_background_skips_the_contrast_check():
    assert QRDesign(color="#fefefe", background="transparent").color == "#fefefe"


def test_colors_are_stored_lower_case():
    assert QRDesign(color="#0B3D91").color == "#0b3d91"


# --- API ------------------------------------------------------------------------------------------------------


def qr(client, name="Lunch menu", target=TARGET, **design):
    return client.post("/api/qrcodes", json={"name": name, "target": target, "design": design})


def test_creating_answers_201_with_the_id_and_the_svg(client):
    response = qr(client, color="#0b3d91")
    assert response.status_code == 201
    body = response.json()
    assert (body["kind"], body["id"], body["slug"]) == ("qrcode", "id000001", "QRcodes/Lunch-menu")
    assert (body["link_host"], body["short_url"]) == ("ip", "http://testserver/links/id000001")
    assert body["design"]["color"] == "#0b3d91"
    assert body["svg"] == render_svg(body["short_url"], QRDesign(color="#0b3d91"))
    assert body["svg_url"] == "/api/qrcodes/id000001/svg"


def test_the_qr_code_leads_to_the_target(client):
    body = qr(client).json()
    response = client.get(f"/links/{body['id']}", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (302, TARGET)


def test_listing_and_reading_one(client):
    body = qr(client).json()
    listed = client.get("/api/qrcodes").json()
    assert (listed["base_url"], listed["ip_base_url"]) == ("http://redirect.lan", "http://testserver")
    assert [(q["id"], q["svg"]) for q in listed["qrcodes"]] == [(body["id"], None)]  # the list stays light
    assert client.get(f"/api/qrcodes/{body['id']}").json()["svg"] == body["svg"]


def test_the_svg_file(client):
    body = qr(client).json()
    response = client.get(body["svg_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["content-disposition"] == 'inline; filename="Lunch-menu.svg"'
    assert response.text == body["svg"]


def test_changing_the_design_keeps_the_link(client):
    body = qr(client).json()
    response = client.put(
        f"/api/qrcodes/{body['id']}",
        json={"name": "Lunch menu", "target": TARGET, "design": {"module_style": "dots", "eye_style": "circle"}},
    )
    assert response.status_code == 200
    changed = response.json()
    assert (changed["id"], changed["short_url"]) == (body["id"], body["short_url"])
    assert "<circle " in changed["svg"]
    assert changed["svg"] != body["svg"]


def test_a_design_error_is_reported_on_the_design_field(client):
    response = qr(client, color="#f0f0f0")
    assert response.status_code == 422
    assert response.json() == {
        "detail": "Some fields are invalid.",
        "errors": {"design": "The colors are too close to the background: scanners could not read the QR code."},
    }


def test_a_logo_fits_in_the_body_limit(client):
    logo = "data:image/png;base64," + base64.b64encode(b"x" * (250 * 1024)).decode()
    assert qr(client, logo=logo).status_code == 201


def test_a_name_already_used_is_refused(client):
    qr(client)
    assert qr(client, name="Lunch  menu").status_code == 409


def test_the_preview_is_the_qr_code_of_the_future_link(client):
    response = client.post("/api/qrcodes/preview", json={"design": {"module_style": "rounded"}})
    assert response.status_code == 200
    assert response.json()["svg"] == render_svg("http://testserver/links/Preview0", QRDesign(module_style="rounded"))
    by_name = client.post("/api/qrcodes/preview", json={"design": {}, "link_host": "hostname"}).json()["svg"]
    assert by_name == render_svg("http://redirect.lan/links/Preview0", QRDesign())
    body = qr(client).json()
    existing = client.post("/api/qrcodes/preview", json={"id": body["id"], "design": {}}).json()["svg"]
    assert existing == body["svg"]


def test_deleting_and_unknown_ids(client):
    body = qr(client).json()
    assert client.delete(f"/api/qrcodes/{body['id']}").status_code == 204
    for path in (f"/api/qrcodes/{body['id']}", f"/api/qrcodes/{body['id']}/svg"):
        assert client.get(path).status_code == 404


def test_qr_routes_only_touch_qr_codes(client):
    nano = client.post("/api/nanourls", json={"name": "x", "target": TARGET}).json()
    assert client.get(f"/api/qrcodes/{nano['id']}").status_code == 404
    assert client.delete(f"/api/qrcodes/{nano['id']}").status_code == 404


def test_qr_changes_are_logged(client):
    body = qr(client).json()
    client.put(f"/api/qrcodes/{body['id']}", json={"name": "Dinner menu", "target": TARGET, "design": {}})
    client.delete(f"/api/qrcodes/{body['id']}")
    events = client.get("/api/logs", params={"kind": "changes"}).json()["events"]
    assert [(e["action"], e["path"]) for e in events] == [
        ("delete", "QRcodes/Dinner-menu"),
        ("update", "QRcodes/Dinner-menu"),
        ("create", "QRcodes/Lunch-menu"),
    ]


def test_a_qr_code_on_the_hostname_carries_it(client):
    body = client.post("/api/qrcodes", json={"name": "Menu", "target": TARGET, "link_host": "hostname"}).json()
    assert body["short_url"] == "http://redirect.lan/links/id000001"
    assert body["svg"] == render_svg("http://redirect.lan/links/id000001", QRDesign())

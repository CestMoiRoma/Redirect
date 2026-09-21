"""End-to-end scenario on the real app + GUI goldens. Usage: screenshots.py OUT_DIR

Starts the real app in-process on a free port (a copy of fixtures/redirect.db; links on the default hostname,
redirect.lan, so they are stable) and a small local target server, then walks every screen and state of the UI in
headless Chromium, at two viewports. Fails on any page error, unexpected console error, unexpected request
or missing element. Every capture goes through golden.py.
"""

import html
import itertools
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ci"))
import golden  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from seed import FIXTURE_REDIRECTS  # noqa: E402
from support import FakeAnnouncer  # noqa: E402

from redirect.config import Settings  # noqa: E402
from redirect.main import create_app  # noqa: E402

FIXTURE_DB = HERE / "fixtures" / "redirect.db"
# The links use the default hostname, redirect.lan, on port 80: the router below answers for it
LINKS_BASE = "http://redirect.lan"
# NanoURLs and QR codes use Redirect's IP address by default: the one the browser reached, on port 80
IP_BASE = "http://127.0.0.1"
PREFIX = f"{LINKS_BASE}/redirect/"
# Hosts of the targets the scenario follows; Chromium resolves them to the local target server
TARGET_DOMAIN = "intranet.local"
TIMEOUT_MS = 20_000

DESKTOP = ("", {"width": 1280, "height": 820})
MOBILE = ("-mobile", {"width": 390, "height": 844})

# Every capture, in scenario order; each one exists for both viewports
SHOTS = (
    "01-empty",
    "02-list",
    "03-filter",
    "04-filter-none",
    "05-error-slug",
    "06-error-target",
    "07-error-name",
    "08-error-slug-taken",
    "09-created",
    "10-edit",
    "11-edit-slug-warning",
    "12-saved",
    "13-delete-confirm",
    "14-deleted",
    "15-copied",
    "16-copy-failed",
    "17-load-error",
    "18-not-found",
    "19-target",
    "20-create-error",
    "21-edit-gone",
    "22-login",
    "23-login-error",
    "24-api-off",
    "25-key-new",
    "26-key-regenerate",
    "27-key-set",
    "28-open-mode",
    "29-hostname-error",
    "30-hostname-mdns",
    "31-environment",
    "32-logs",
    "33-logs-changes",
    "34-nano-empty",
    "35-nano-created",
    "36-nano-name-taken",
    "37-qr-empty",
    "38-qr-designed",
    "39-qr-created",
    "40-qr-contrast",
    "41-denied",
    "42-access-off",
    "43-headless",
)

# Web fonts each kind of page must have loaded (and so rendered with) when captured
INTER = ("Inter 400", "Inter 600")
ALL_FONTS = (*INTER, "JetBrains Mono 400", "JetBrains Mono 600")
FORM_FONTS = (*INTER, "JetBrains Mono 400")

# The fixture, as the list shows it (sorted by name)
FIXTURE_ROWS = (
    ("Budget 2026", "compta/budget-2026", "http://files.intranet.local/compta/Budget%202026.xlsx"),
    ("Redirect api", "local-api/Test1", "http://10.12.75.1/api/test1"),
    ("Team wiki", "wiki", "http://wiki.intranet.local/start"),
)

# The scenario runs with an admin password, as a real deployment should; 28-open-mode runs without
ADMIN_PASSWORD = "correct horse battery"
# API keys are random in the app; the scenario injects these, in this order, so the captures are stable
KEY_BODIES = ("K1kE9rT3xQ7mW2vN8pL4sD6fH0jB5cY1uZ3aG7eR9tX", "M4nP8qS2wV6yB0dF3hJ7kL1oR5tU9xA2cE6gI0mQ4sW")
FIRST_KEY, SECOND_KEY = (f"rdk_{body}" for body in KEY_BODIES)

# Ids of the fixture's redirects (seed.py)
FIXTURE_IDS = {slug: code for code, slug, _, _ in FIXTURE_REDIRECTS}

SLUG_INVALID = "Use only letters, digits, dots, dashes and underscores, with / between segments."
TARGET_INVALID = "Enter a full URL that starts with http:// or https://."

TARGET_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Target reached</title>
    <style>
      body {{ margin: 0; background: #ffffff; color: #1b1f24; font: 16px/1.5 'DejaVu Sans', sans-serif; }}
      main {{ padding: 32px 24px; }}
      h1 {{ margin: 0 0 8px; font-size: 24px; }}
      code {{ font: 15px 'DejaVu Sans Mono', monospace; overflow-wrap: anywhere; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Target reached</h1>
      <p>This page stands for the target of a redirect.</p>
      <p><code>{url}</code></p>
    </main>
  </body>
</html>
"""


# --- Servers -------------------------------------------------------------------------------------------


@contextmanager
def app_server(
    data_dir: Path,
    password: str | None = ADMIN_PASSWORD,
    public_base_url: str | None = None,
    api_enabled: bool | None = None,
    local_only: bool | None = None,
    headless: bool = False,
) -> Iterator[str]:
    """The real app, in this process, on a free port. Backend clock frozen at the golden time, fixed API keys,
    and an mDNS announcer that stays off the network."""
    settings = Settings(
        data_dir=data_dir,
        public_base_url=public_base_url,
        log_level="WARNING",
        admin_password=password,
        api_enabled=api_enabled,
        local_only=local_only,
        headless=headless,
    )
    bodies = itertools.cycle(KEY_BODIES)
    ids = (f"New{number:05d}" for number in itertools.count(1))
    app = create_app(
        settings,
        clock=lambda: golden.GOLDEN_TIME,
        new_key=lambda: next(bodies),
        announcer=FakeAnnouncer(),
        make_code=lambda: next(ids),
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the app did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()


class _TargetHandler(BaseHTTPRequestHandler):
    """A fixed page that shows the URL it was reached at."""

    def do_GET(self) -> None:
        body = TARGET_PAGE.format(url=html.escape(f"http://{self.headers['Host']}{self.path}")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # quiet: the scenario prints its own progress


@contextmanager
def target_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


# --- Browser checks ------------------------------------------------------------------------------------


class Watch:
    """Collects page errors, console errors and unexpected requests. Expected HTTP errors are declared."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self._expected: list[dict] = []

    def attach(self, page: Page) -> None:
        page.on("pageerror", lambda error: self.errors.append(f"pageerror on {page.url}: {error}"))
        page.on("console", self._console)

    def _console(self, message) -> None:
        if message.type != "error":
            return
        url = message.location.get("url", "")
        for entry in self._expected:
            if not entry["seen"] and url == entry["url"] and message.text.startswith(entry["text"]):
                entry["seen"] = True
                return
        self.errors.append(f"console.error: {message.text} ({url})")
        # Printed now too, between the capture names: the log then shows where it happened
        print(f"  unexpected {self.errors[-1]}", flush=True)

    @contextmanager
    def http_error(self, page: Page, url: str, status: int) -> Iterator[None]:
        """Inside the block, Chromium must log one failed load of `url` with `status`.

        Chromium logs every non-2xx response as a console error. Each expected one is declared here, by URL
        and status, and must happen; anything else still fails the scenario.
        """
        with self._expect(page, url, f"Failed to load resource: the server responded with a status of {status} "):
            yield

    @contextmanager
    def network_error(self, page: Page, url: str) -> Iterator[None]:
        """Inside the block, Chromium must log one request to `url` that failed without a response."""
        with self._expect(page, url, "Failed to load resource: net::ERR_FAILED"):
            yield

    @contextmanager
    def _expect(self, page: Page, url: str, text: str) -> Iterator[None]:
        entry = {"url": url, "text": text, "seen": False}
        self._expected.append(entry)
        try:
            yield
            deadline = time.monotonic() + 10
            while not entry["seen"] and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            if not entry["seen"]:
                raise AssertionError(f"expected the console error {text!r} for {url}")
        finally:
            self._expected.remove(entry)


def loaded_fonts(page: Page) -> set[str]:
    return set(
        page.evaluate(
            """() => [...document.fonts]
                .filter((face) => face.status === 'loaded')
                .map((face) => `${face.family.replace(/["']/g, '')} ${face.weight}`)"""
        )
    )


def expect_web_fonts(page: Page, faces: tuple[str, ...]) -> None:
    """The self-hosted fonts are loaded, so the text is drawn with them and not with a system fallback."""
    missing = set(faces) - loaded_fonts(page)
    assert not missing, f"web fonts not loaded on {page.url}: {sorted(missing)}"
    specs = {"Inter 400": "15px Inter", "Inter 600": "600 15px Inter"}
    specs |= {"JetBrains Mono 400": '15px "JetBrains Mono"', "JetBrains Mono 600": '600 15px "JetBrains Mono"'}
    for face in faces:
        assert page.evaluate("(spec) => document.fonts.check(spec)", specs[face]), (
            f"document.fonts.check failed: {face}"
        )


class Scenario:
    def __init__(self, browser, out_dir: Path, suffix: str, viewport: dict, watch: Watch) -> None:
        self.browser = browser
        self.out_dir = out_dir
        self.suffix = suffix
        self.viewport = viewport
        self.watch = watch
        self.served: list[tuple[str, int]] = []

    def open(self, base: str) -> Page:
        page = golden.new_page(self.browser, self.viewport)
        page.set_default_timeout(TIMEOUT_MS)
        context = page.context
        context.on("page", self.watch.attach)
        self.watch.attach(page)
        context.route("**/*", self._router(base))
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        return page

    def _router(self, base: str) -> Callable:
        def handle(route) -> None:
            url = urlsplit(route.request.url)
            origin = f"{url.scheme}://{url.netloc}"
            if origin == base or (url.hostname or "").endswith("." + TARGET_DOMAIN):
                route.continue_()
            elif origin in (LINKS_BASE, IP_BASE):
                # The public host does not resolve: answer from the real app, redirects left to the browser
                path = url.path + (f"?{url.query}" if url.query else "")
                response = route.fetch(url=base + path, max_redirects=0)
                self.served.append((route.request.url, response.status))
                route.fulfill(response=response)
            else:
                self.watch.errors.append(f"unexpected request: {route.request.method} {route.request.url}")
                route.abort()

        return handle

    def shot(self, page: Page, name: str, *, full_page: bool = True, fonts: tuple[str, ...] = ALL_FONTS) -> None:
        assert name in SHOTS, name
        page.mouse.move(0, 0)
        if full_page:
            page.evaluate("() => window.scrollTo(0, 0)")
        golden.settle(page)
        expect_web_fonts(page, fonts)
        golden.screenshot(page, self.out_dir / f"{name}{self.suffix}.png", full_page=full_page)
        print(f"  {name}{self.suffix}.png", flush=True)


# --- The walk ------------------------------------------------------------------------------------------


def expect_header(page: Page) -> None:
    expect(page).to_have_title("Redirect")
    expect(page.get_by_role("heading", level=1)).to_have_text("Redirect")
    expect(page.locator(".tagline")).to_have_text("Fixed links that point wherever you need them to.")


def id_of(page: Page, base: str, slug: str) -> str:
    """The id of a redirect, from the API list (ids of created entries depend on the scenario's path)."""
    redirects = page.request.get(base + "/api/list").json()["redirects"]
    return next(r["id"] for r in redirects if r["slug"] == slug)


def log_in(page: Page) -> None:
    """From the login screen to the app, with the right password."""
    view = page.locator("#login-view")
    expect(view).to_be_visible()
    view.get_by_label("Password").fill(ADMIN_PASSWORD)
    view.get_by_role("button", name="Log in").click()
    expect(page.locator("#main")).to_be_visible()
    expect(view).to_be_hidden()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.locator("#open-warning")).to_be_hidden()


def expect_create_form(page: Page) -> None:
    create = page.locator("#create")
    expect(create.get_by_role("heading", level=2)).to_have_text("New redirect")
    expect(create.locator("label")).to_have_text(["Path", "Name", "Target URL"])
    expect(create.locator(".prefix")).to_have_text(PREFIX)
    expect(create.locator(".hint")).to_have_text(
        [
            "Letters, digits, dots, dashes and underscores. Use / to group, as in compta/budget-2026.",
            "Only shown here, to find the redirect again.",
            "Where the link sends people. You can change it at any time.",
        ]
    )
    expect(create.get_by_role("button")).to_have_text(["Create redirect"])
    expect(create.locator(".field-error")).to_have_count(3)
    for error in create.locator(".field-error").all():
        expect(error).to_be_hidden()


def expect_rows(page: Page, rows: tuple[tuple[str, str, str], ...]) -> None:
    items = page.locator("#list .item")
    expect(items).to_have_count(len(rows))
    expect(items.locator(".link")).to_have_text([PREFIX + slug for _, slug, _ in rows])
    expect(items.locator(".item-name")).to_have_text([name for name, _, _ in rows])
    expect(items.locator(".target-url")).to_have_text([target for _, _, target in rows])
    # Every entry has its short link, /links/<id>; the fixture's ids are known
    expect(items.locator(".short-link")).to_have_count(len(rows))
    for index, (name, slug, _) in enumerate(rows):
        item = items.nth(index)
        short = item.locator(".short-link")
        expect(short).to_have_text(re.compile(r"^http://[\w.-]+/links/[A-Za-z0-9]{8}$"))
        if slug in FIXTURE_IDS:
            expect(short).to_have_text(f"{LINKS_BASE}/links/{FIXTURE_IDS[slug]}")
        expect(item.locator(".link")).to_have_attribute("href", PREFIX + slug)
        expect(item.locator(".link")).to_have_attribute("target", "_blank")
        expect(item.get_by_role("button", name=f"Copy link: {name}", exact=True)).to_have_text("Copy link")
        expect(item.get_by_role("button", name=f"Edit: {name}", exact=True)).to_have_text("Edit")
        expect(item.get_by_role("button", name=f"Delete: {name}", exact=True)).to_have_text("Delete")


def assert_no_horizontal_overflow(page: Page, where: str) -> None:
    """Nothing makes the page, or an open dialog, wider than the viewport."""
    page_width, viewport = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
    assert page_width <= viewport, f"{where}: the page is {page_width}px wide in a {viewport}px viewport"
    for dialog in page.locator("dialog[open]").all():
        scroll, client = dialog.evaluate("(d) => [d.scrollWidth, d.clientWidth]")
        assert scroll == client, f"{where}: an open dialog scrolls sideways ({scroll}px in {client}px)"


def assert_aligned(page: Page, selector: str, reference: str) -> None:
    """Left edges of two elements line up (same page margins)."""
    box, ref = page.locator(selector).bounding_box(), page.locator(reference).bounding_box()
    assert box is not None and ref is not None and box["x"] == ref["x"], f"{selector} {box} vs {reference} {ref}"


def empty_list(run: Scenario, data_dir: Path) -> None:
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        expect_header(page)
        expect(page.locator("#main")).to_be_visible()
        expect(page.get_by_role("heading", level=2, name="Redirects")).to_be_visible()
        expect(page.locator("#empty p")).to_have_text(["No redirects yet", "Fill in the form to create the first one."])
        expect(page.locator("#count")).to_be_hidden()
        expect(page.locator("#filter-box")).to_be_hidden()
        expect(page.locator("#list .item")).to_have_count(0)
        expect(page.locator("#status")).to_be_hidden()
        expect_create_form(page)
        run.shot(page, "01-empty", fonts=FORM_FONTS)
        page.context.close()


def main_walk(run: Scenario, data_dir: Path) -> None:
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        items = page.locator("#list .item")
        status = page.locator("#status")

        # --- The list
        page.goto(base + "/")
        log_in(page)
        expect_header(page)
        expect(page.locator("#count")).to_have_text("3 redirects")
        expect(page.locator("#filter")).to_have_attribute("placeholder", "Filter by name, path or target")
        # Wide enough for its placeholder on desktop, full width on mobile
        expect(page.locator("#filter")).to_have_css("width", "280px" if run.suffix == DESKTOP[0] else "362px")
        expect(page.locator("#empty")).to_be_hidden()
        expect(status).to_be_hidden()
        expect_rows(page, FIXTURE_ROWS)
        expect_create_form(page)
        run.shot(page, "02-list")

        # --- Filter: name, path, target, public link; case-insensitive
        search = page.get_by_role("searchbox", name="Filter")
        search.fill("wiki")
        expect_rows(page, FIXTURE_ROWS[2:])
        expect(page.locator("#count")).to_have_text("3 redirects")
        expect(page.locator("#no-match")).to_be_hidden()
        run.shot(page, "03-filter")
        for query, rows in (
            ("10.12.75", FIXTURE_ROWS[1:2]),
            ("COMPTA/", FIXTURE_ROWS[:1]),
            ("redirect.lan", FIXTURE_ROWS),
        ):
            search.fill(query)
            expect_rows(page, rows)
        search.fill("payroll")
        expect(items).to_have_count(0)
        expect(page.locator("#no-match")).to_have_text("No redirect matches “payroll”.")
        run.shot(page, "04-filter-none")
        search.fill("")
        expect_rows(page, FIXTURE_ROWS)
        expect(page.locator("#no-match")).to_be_hidden()

        # --- Create: every validation error, then success
        form = page.locator("#create-form")
        slug, name, target = (
            form.get_by_label("Path"),
            form.get_by_label("Name", exact=True),
            form.get_by_label("Target URL"),
        )
        errors = {field: page.locator(f"#create-{field}-error") for field in ("slug", "name", "target")}
        submit = form.get_by_role("button", name="Create redirect")

        def submit_invalid(field: str, message: str, status_code: int = 422) -> None:
            with run.watch.http_error(page, base + "/api/redirects", status_code):
                submit.click()
                expect(errors[field]).to_have_text(message)
            for other, error in errors.items():
                if other != field:
                    expect(error).to_be_hidden()
            invalid = {"slug": slug, "name": name, "target": target}[field]
            expect(invalid).to_have_attribute("aria-invalid", "true")
            expect(invalid).to_be_focused()
            expect(form.locator(".form-error")).to_be_hidden()
            expect(submit).to_be_enabled()
            expect(items).to_have_count(3)

        slug.fill("compta/budget 2027")
        name.fill("Budget 2027")
        target.fill("http://files.intranet.local/compta/Budget%202027.xlsx")
        submit_invalid("slug", SLUG_INVALID)
        run.shot(page, "05-error-slug")

        slug.fill("compta/budget-2027")
        target.fill("files.intranet.local/compta/Budget 2027.xlsx")
        submit_invalid("target", TARGET_INVALID)
        expect(slug).not_to_have_attribute("aria-invalid", "true")
        run.shot(page, "06-error-target")

        target.fill("http://files.intranet.local/compta/Budget%202027.xlsx")
        name.fill("   ")
        submit_invalid("name", "Enter a name.")
        run.shot(page, "07-error-name")

        name.fill("Budget 2027")
        slug.fill("compta/budget-2026")
        submit_invalid("slug", "This path is already used by another redirect.", status_code=409)
        run.shot(page, "08-error-slug-taken")

        slug.fill("compta/budget-2027")
        submit.click()
        expect(status).to_have_text("Redirect “Budget 2027” created.")
        created = ("Budget 2027", "compta/budget-2027", "http://files.intranet.local/compta/Budget%202027.xlsx")
        rows = (FIXTURE_ROWS[0], created, *FIXTURE_ROWS[1:])
        expect_rows(page, rows)
        expect(page.locator("#count")).to_have_text("4 redirects")
        expect(page.locator("#list .item.is-new")).to_have_count(1)
        expect(items.nth(1)).to_have_class("item is-new")
        for field in (slug, name, target):
            expect(field).to_have_value("")
        for error in errors.values():
            expect(error).to_be_hidden()
        expect(items.nth(1).get_by_role("button", name="Copy link: Budget 2027")).to_be_focused()
        run.shot(page, "09-created")

        # --- Edit: focus on the target, cancel, slug warning, a validation error, save
        dialog = page.locator("#edit-dialog")
        edit_button = items.nth(1).get_by_role("button", name="Edit: Budget 2027")
        edit_button.click()
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Cancel").click()
        expect(dialog).to_be_hidden()
        expect(edit_button).to_be_focused()
        edit_button.click()
        expect(dialog).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        edit_button.click()
        expect(dialog.get_by_role("heading", level=2)).to_have_text("Edit redirect")
        expect(dialog.locator("label")).to_have_text(["Path", "Name", "Target URL"])
        expect(dialog.locator(".prefix")).to_have_text(PREFIX)
        edit_slug, edit_name, edit_target = (dialog.get_by_label(label) for label in ("Path", "Name", "Target URL"))
        expect(edit_slug).to_have_value("compta/budget-2027")
        expect(edit_name).to_have_value("Budget 2027")
        expect(edit_target).to_have_value("http://files.intranet.local/compta/Budget%202027.xlsx")
        expect(edit_target).to_be_focused()
        warning = dialog.locator("#edit-slug-warning")
        # A live region, empty (so neither shown nor part of the field's description) until the path changes
        expect(warning).to_have_attribute("aria-live", "polite")
        expect(warning).to_have_text("")
        expect(edit_slug).to_have_accessible_description("")
        expect(dialog.get_by_role("button")).to_have_text(["Cancel", "Save changes"])
        run.shot(page, "10-edit", full_page=False)

        edit_slug.fill("compta/2027/budget")
        slug_warning = "Changing the path breaks the link you already shared. The old link will return “not found”."
        expect(warning).to_have_text(slug_warning)
        expect(edit_slug).to_have_accessible_description(slug_warning)
        edit_slug.fill("compta/budget-2027")
        expect(warning).to_have_text("")
        expect(edit_slug).to_have_accessible_description("")
        edit_slug.fill("compta/2027/budget")
        expect(warning).to_have_text(slug_warning)
        edit_target.fill("http://files.intranet.local/compta/2027/Budget.xlsx")
        run.shot(page, "11-edit-slug-warning", full_page=False)

        # A validation error in the dialog is shown there, and the dialog stays open
        save = dialog.get_by_role("button", name="Save changes")
        edit_target.fill("sharepoint/budget.xlsx")
        with run.watch.http_error(page, base + f"/api/redirects/{id_of(page, base, 'compta/budget-2027')}", 422):
            save.click()
            expect(dialog.locator("#edit-target-error")).to_have_text(TARGET_INVALID)
        expect(dialog).to_be_visible()
        expect(edit_target).to_be_focused()
        expect(warning).to_have_text(slug_warning)
        edit_target.fill("http://files.intranet.local/compta/2027/Budget.xlsx")
        save.click()
        expect(dialog).to_be_hidden()
        expect(status).to_have_text("Redirect “Budget 2027” saved.")
        saved = ("Budget 2027", "compta/2027/budget", "http://files.intranet.local/compta/2027/Budget.xlsx")
        rows = (FIXTURE_ROWS[0], saved, *FIXTURE_ROWS[1:])
        expect_rows(page, rows)
        expect(items.nth(1)).to_have_class("item is-new")
        expect(items.nth(1).get_by_role("button", name="Edit: Budget 2027")).to_be_focused()
        run.shot(page, "12-saved")

        # --- Delete: keep it, then delete
        delete_dialog = page.locator("#delete-dialog")
        delete_button = items.nth(1).get_by_role("button", name="Delete: Budget 2027")
        delete_button.click()
        expect(delete_dialog).to_be_visible()
        delete_dialog.get_by_role("button", name="Keep it").click()
        expect(delete_dialog).to_be_hidden()
        expect(delete_button).to_be_focused()
        expect(items).to_have_count(4)
        delete_button.click()
        expect(delete_dialog.get_by_role("heading", level=2)).to_have_text("Delete this redirect?")
        expect(delete_dialog.locator("#delete-name")).to_have_text("Budget 2027")
        expect(delete_dialog.locator("#delete-text")).to_have_text(
            f"The link {PREFIX}compta/2027/budget will stop working for everyone who has it."
        )
        expect(delete_dialog.get_by_role("button")).to_have_text(["Keep it", "Delete"])
        expect(delete_dialog.get_by_role("button", name="Keep it")).to_be_focused()
        run.shot(page, "13-delete-confirm", full_page=False)

        delete_dialog.get_by_role("button", name="Delete").click()
        expect(delete_dialog).to_be_hidden()
        expect(status).to_have_text("Redirect “Budget 2027” deleted.")
        expect_rows(page, FIXTURE_ROWS)
        expect(page.locator("#count")).to_have_text("3 redirects")
        expect(page.locator("#list .item.is-new")).to_have_count(0)
        expect(page.locator("#list-title")).to_be_focused()
        run.shot(page, "14-deleted")

        # --- Copy: success (clipboard checked), the button comes back after 2 s, then a failed copy
        copy = items.nth(0).get_by_role("button", name="Copy link: Budget 2026")
        copy.click()
        expect(copy).to_have_text("Copied")
        expect(copy).to_have_class("button copy is-copied")
        expect(status).to_have_text("Link copied to the clipboard.")
        expect(status).not_to_have_class("status status-error")
        assert page.evaluate("() => navigator.clipboard.readText()") == PREFIX + "compta/budget-2026"
        run.shot(page, "15-copied")
        page.clock.run_for(1999)
        expect(copy).to_have_text("Copied")
        page.clock.run_for(1)
        expect(copy).to_have_text("Copy link")
        expect(copy).to_have_class("button copy")

        page.evaluate(
            """() => {
              navigator.clipboard.writeText = () => Promise.reject(new DOMException('Denied', 'NotAllowedError'))
              document.execCommand = () => false
            }"""
        )
        failed = items.nth(1).get_by_role("button", name="Copy link: Redirect api")
        failed.click()
        expect(failed).to_have_text("Copy failed")
        expect(failed).to_have_class("button copy is-failed")
        expect(status).to_have_text("Could not copy the link. Select it in the list and copy it by hand.")
        expect(status).to_have_class("status status-error")
        assert page.evaluate("() => navigator.clipboard.readText()") == PREFIX + "compta/budget-2026"
        run.shot(page, "16-copy-failed")
        page.clock.run_for(2000)
        expect(failed).to_have_text("Copy link")

        # --- Copy over plain http (a LAN address): no Clipboard API, the execCommand fallback copies
        lan = page.context.new_page()
        lan.goto(LINKS_BASE + "/")
        # No login here: the router answers for the public host with route.fetch, whose requests to the real
        # app carry this context's session cookie
        expect(lan.locator("#main")).to_be_visible()
        assert lan.evaluate("() => window.isSecureContext") is False
        lan_copy = lan.locator("#list .item").nth(2).get_by_role("button", name="Copy link: Team wiki")
        lan_copy.click()
        expect(lan_copy).to_have_text("Copied")
        lan.close()
        page.bring_to_front()
        page.reload()
        assert page.evaluate("() => navigator.clipboard.readText()") == PREFIX + "wiki"
        # Let this load finish (its settings come last) before the next step answers /api/redirects with a 500:
        # a request still on its way would get that 500 too
        expect_rows(page, FIXTURE_ROWS)
        expect(page.locator("#settings-hostname")).to_have_value("redirect.lan")

        # --- The list cannot be loaded
        api = base + "/api/redirects"
        page.route(api, lambda route: route.fulfill(status=500, json={"detail": "Internal Server Error", "errors": {}}))
        with run.watch.http_error(page, api, 500):
            page.reload()
            expect(page.locator("#load-error")).to_have_text(
                "Could not load the redirects. Reload the page to try again."
            )
        expect(page.locator("#main")).to_be_hidden()
        assert_aligned(page, "#load-error", ".brand")
        run.shot(page, "17-load-error", fonts=INTER)
        page.unroute(api)
        page.reload()
        expect_rows(page, FIXTURE_ROWS)

        # --- A link that no longer exists (renamed, then deleted above)
        gone = base + "/redirect/compta/budget-2027"
        with run.watch.http_error(page, gone, 404):
            response = page.goto(gone)
        assert response is not None and response.status == 404
        expect(page).to_have_title("Link not found")
        expect(page.get_by_role("heading", level=1)).to_have_text("Link not found")
        expect(page.locator(".standalone p")).to_have_text(
            "No redirect exists for /redirect/compta/budget-2027. The link may have been changed or removed."
        )
        back = page.get_by_role("link", name="Open Redirect")
        expect(back).to_have_attribute("href", "/")
        run.shot(page, "18-not-found", full_page=False, fonts=INTER)
        back.click()
        expect_rows(page, FIXTURE_ROWS)

        # --- Follow a public link: 302 from Redirect, then the target page
        with page.expect_popup() as popup_info:
            items.nth(2).get_by_role("link", name=PREFIX + "wiki").click()
        landed = popup_info.value
        expect(landed).to_have_url("http://wiki.intranet.local/start")
        expect(landed.get_by_role("heading", level=1)).to_have_text("Target reached")
        expect(landed.locator("code")).to_have_text("http://wiki.intranet.local/start")
        assert run.served[-1] == (PREFIX + "wiki", 302), run.served
        run.shot(landed, "19-target", full_page=False, fonts=())
        landed.close()

        # --- Change the target in the UI: the same link now lands on the new target
        items.nth(2).get_by_role("button", name="Edit: Team wiki").click()
        dialog.get_by_label("Target URL").fill("http://wiki.intranet.local/home")
        dialog.get_by_role("button", name="Save changes").click()
        expect(status).to_have_text("Redirect “Team wiki” saved.")
        with page.expect_popup() as popup_info:
            items.nth(2).get_by_role("link", name=PREFIX + "wiki").click()
        landed = popup_info.value
        expect(landed).to_have_url("http://wiki.intranet.local/home")
        expect(landed.locator("code")).to_have_text("http://wiki.intranet.local/home")
        landed.close()
        wiki_home = (*FIXTURE_ROWS[2][:2], "http://wiki.intranet.local/home")
        rows = (*FIXTURE_ROWS[:2], wiki_home)
        expect_rows(page, rows)

        # --- Creating while the server cannot be reached: the error shows in the form, the focus stays there
        slug.fill("compta/budget-2028")
        name.fill("Budget 2028")
        target.fill("http://files.intranet.local/compta/Budget%202028.xlsx")
        page.route(api, lambda route: route.abort() if route.request.method == "POST" else route.fallback())
        with run.watch.network_error(page, api):
            submit.click()
            expect(form.locator(".form-error")).to_have_text("Could not reach the server. Try again.")
        expect(form.locator(".form-error")).to_have_attribute("role", "alert")
        # The status of the previous action ("saved") is gone: it would contradict the error
        expect(status).to_be_hidden()
        for error in errors.values():
            expect(error).to_be_hidden()
        expect(submit).to_be_enabled()
        expect(submit).to_be_focused()
        expect_rows(page, rows)
        run.shot(page, "20-create-error")
        page.unroute(api)

        # A response without an API message (a proxy's 500 page) is a server error, not an unreachable server
        page.route(
            api,
            lambda route: (
                route.fulfill(status=500, content_type="text/plain", body="Internal Server Error")
                if route.request.method == "POST"
                else route.fallback()
            ),
        )
        with run.watch.http_error(page, api, 500):
            submit.click()
            expect(form.locator(".form-error")).to_have_text("The server could not complete the request. Try again.")
        expect(submit).to_be_focused()
        page.unroute(api)
        submit.click()
        expect(status).to_have_text("Redirect “Budget 2028” created.")
        expect(form.locator(".form-error")).to_be_hidden()
        rows = (
            FIXTURE_ROWS[0],
            ("Budget 2028", "compta/budget-2028", "http://files.intranet.local/compta/Budget%202028.xlsx"),
            *rows[1:],
        )
        expect_rows(page, rows)

        # --- The entry being edited is deleted elsewhere: the dialog closes, the list and the status tell
        items.nth(3).get_by_role("button", name="Edit: Team wiki").click()
        expect(dialog).to_be_visible()
        wiki_api = base + f"/api/redirects/{FIXTURE_IDS['wiki']}"
        assert page.request.delete(wiki_api).status == 204
        with run.watch.http_error(page, wiki_api, 404):
            dialog.get_by_role("button", name="Save changes").click()
            expect(status).to_have_text("This redirect no longer exists.")
        expect(dialog).to_be_hidden()
        expect(status).to_have_class("status status-error")
        rows = rows[:3]
        expect_rows(page, rows)
        expect(page.locator("#count")).to_have_text("3 redirects")
        # The error is about another entry: the last created one is no longer highlighted
        expect(page.locator("#list .item.is-new")).to_have_count(0)
        expect(page.locator("#list-title")).to_be_focused()
        run.shot(page, "21-edit-gone")

        # The same for a delete: the entry is already gone, the status says so, the list catches up
        items.nth(2).get_by_role("button", name="Delete: Redirect api").click()
        expect(delete_dialog).to_be_visible()
        api_entry = base + f"/api/redirects/{FIXTURE_IDS['local-api/Test1']}"
        assert page.request.delete(api_entry).status == 204
        # Budget 2028 goes too, leaving a single entry
        budget_2028 = base + f"/api/redirects/{id_of(page, base, 'compta/budget-2028')}"
        assert page.request.delete(budget_2028).status == 204
        with run.watch.http_error(page, api_entry, 404):
            delete_dialog.get_by_role("button", name="Delete").click()
            expect(status).to_have_text("This redirect no longer exists.")
        expect(delete_dialog).to_be_hidden()
        expect(status).to_have_class("status status-error")
        expect_rows(page, FIXTURE_ROWS[:1])
        # One entry left: the count is singular
        expect(page.locator("#count")).to_have_text("1 redirect")
        expect(page.locator("#list-title")).to_be_focused()

        # --- User text of any length never widens the page, nor a dialog
        assert_no_horizontal_overflow(page, "the list")
        long_name = "Quarterly" + "N" * 91
        slug.fill("long")
        name.fill(long_name)
        target.fill("http://files.intranet.local/" + "a" * 300)
        submit.click()
        expect(status).to_have_text(f"Redirect “{long_name}” created.")
        expect(items).to_have_count(2)
        assert_no_horizontal_overflow(page, "a 100-character name")
        search.fill("http://files.intranet.local/compta/Budget%202027.xlsx")
        expect(page.locator("#no-match")).to_be_visible()
        assert_no_horizontal_overflow(page, "a long filter without spaces")
        search.fill("")
        items.nth(1).get_by_role("button", name=f"Delete: {long_name}").click()
        expect(delete_dialog.locator("#delete-name")).to_have_text(long_name)
        assert_no_horizontal_overflow(page, "the delete dialog of a 100-character name")
        delete_dialog.get_by_role("button", name="Keep it").click()
        items.nth(1).get_by_role("button", name=f"Edit: {long_name}").click()
        assert_no_horizontal_overflow(page, "the edit dialog of a 100-character name")
        page.keyboard.press("Escape")

        page.context.close()


def auth_walk(run: Scenario, data_dir: Path) -> None:
    """Login, the API key (generate, copy, use, regenerate), logout, a session lost while working."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        view = page.locator("#login-view")
        password = view.get_by_label("Password")
        log_in_button = view.get_by_role("button", name="Log in")

        # --- The login screen, then a wrong password
        page.goto(base + "/")
        expect_header(page)
        expect(view).to_be_visible()
        expect(page.locator("#main")).to_be_hidden()
        expect(page.locator("#logout")).to_be_hidden()
        expect(page.locator("#open-warning")).to_be_hidden()
        expect(view.get_by_role("heading", level=2)).to_have_text("Log in")
        expect(view.locator(".hint")).to_have_text("Enter the admin password to manage the redirects.")
        expect(password).to_have_attribute("type", "password")
        expect(password).to_have_attribute("autocomplete", "current-password")
        expect(password).to_be_focused()
        run.shot(page, "22-login", fonts=INTER)

        password.fill("wrong password")
        with run.watch.http_error(page, base + "/api/login", 401):
            log_in_button.click()
            expect(view.locator("#login-password-error")).to_have_text("Wrong password.")
        expect(password).to_have_attribute("aria-invalid", "true")
        expect(password).to_be_focused()
        expect(page.locator("#main")).to_be_hidden()
        run.shot(page, "23-login-error", fonts=INTER)

        password.fill(ADMIN_PASSWORD)
        log_in_button.click()
        expect(page.locator("#main")).to_be_visible()
        expect(view).to_be_hidden()
        expect_rows(page, FIXTURE_ROWS)

        # --- The API is off by default: scripts are refused, no key to manage
        card = page.locator("#key-card")
        state = card.locator("#key-state")
        toggle = card.get_by_label("Let scripts use the API")
        expect(card.get_by_role("heading", level=2)).to_have_text("API")
        expect(toggle).not_to_be_checked()
        expect(toggle).to_be_enabled()
        expect(card.locator("#api-off")).to_have_text("Scripts are refused while the API is off.")
        expect(card.locator("#key-section")).to_be_hidden()
        expect(card.locator("#api-forced")).to_be_hidden()
        run.shot(page, "24-api-off")

        # --- Turn it on: no key yet
        toggle.check()
        expect(card.locator("#key-section")).to_be_visible()
        expect(card.locator("#api-off")).to_be_hidden()
        expect(card.locator("#key-section .hint")).to_have_text(
            "Scripts send it as Authorization: Bearer <key> to /api/redirects, to create, change or delete redirects."
        )
        expect(state).to_have_text("No API key yet.")
        expect(toggle).to_be_focused()

        # --- Generate: the key is shown once, copied, then gone from the page
        card.get_by_role("button", name="Generate a key").click()
        new_dialog = page.locator("#key-new-dialog")
        expect(new_dialog).to_be_visible()
        expect(new_dialog.get_by_role("heading", level=2)).to_have_text("Your new API key")
        expect(new_dialog.locator("#key-new-text")).to_have_text(
            "Copy it now: it is not stored and will not be shown again."
        )
        value = new_dialog.get_by_label("API key")
        expect(value).to_have_value(FIRST_KEY)
        expect(value).to_be_focused()
        expect(new_dialog.get_by_role("button", name="Copy key")).to_be_visible()
        copy_key = new_dialog.locator("#key-new-copy")  # by id: its name changes to "Copied"
        copy_key.click()
        expect(copy_key).to_have_text("Copied")
        assert page.evaluate("() => navigator.clipboard.readText()") == FIRST_KEY
        run.shot(page, "25-key-new", full_page=False)
        new_dialog.get_by_role("button", name="Done").click()
        expect(new_dialog).to_be_hidden()
        expect(value).to_have_value("")
        regenerate = card.get_by_role("button", name="Regenerate the key")
        expect(regenerate).to_be_focused()
        expect(state).to_have_text("Created on 2026-01-15. It starts with rdk_K1kE…")

        # A script with the key and no session: the key opens the redirects API, nothing else does
        script = run.browser.new_context()
        api = base + "/api/redirects"
        assert script.request.get(api, headers={"Authorization": f"Bearer {FIRST_KEY}"}).status == 200
        assert script.request.get(api).status == 401

        # --- Regenerate: confirm first, keep the key once, then replace it
        confirm = page.locator("#key-confirm-dialog")
        regenerate.click()
        expect(confirm).to_be_visible()
        expect(confirm.get_by_role("heading", level=2)).to_have_text("Regenerate the API key?")
        expect(confirm.locator("#key-confirm-text")).to_have_text(
            "The current key stops working at once, for every script that uses it."
        )
        expect(confirm.get_by_role("button")).to_have_text(["Keep the current key", "Regenerate"])
        expect(confirm.get_by_role("button", name="Keep the current key")).to_be_focused()
        run.shot(page, "26-key-regenerate", full_page=False)
        confirm.get_by_role("button", name="Keep the current key").click()
        expect(confirm).to_be_hidden()
        expect(state).to_have_text("Created on 2026-01-15. It starts with rdk_K1kE…")
        regenerate.click()
        confirm.get_by_role("button", name="Regenerate").click()
        expect(value).to_have_value(SECOND_KEY)
        new_dialog.get_by_role("button", name="Done").click()
        expect(state).to_have_text("Created on 2026-01-15. It starts with rdk_M4nP…")
        assert script.request.get(api, headers={"Authorization": f"Bearer {FIRST_KEY}"}).status == 401
        assert script.request.get(api, headers={"Authorization": f"Bearer {SECOND_KEY}"}).status == 200
        run.shot(page, "27-key-set")

        # --- Turn the API off: the key is refused, the interface keeps working; then on again
        toggle.uncheck()
        expect(card.locator("#key-section")).to_be_hidden()
        assert script.request.get(api, headers={"Authorization": f"Bearer {SECOND_KEY}"}).status == 403
        toggle.check()
        # Shown again once the server has saved the change
        expect(card.locator("#key-section")).to_be_visible()
        expect(state).to_have_text("Created on 2026-01-15. It starts with rdk_M4nP…")
        assert script.request.get(api, headers={"Authorization": f"Bearer {SECOND_KEY}"}).status == 200
        script.close()

        # --- Log out: back to the login screen, for good
        page.get_by_role("button", name="Log out").click()
        expect(view).to_be_visible()
        expect(page.locator("#main")).to_be_hidden()
        expect(password).to_be_focused()
        page.reload()
        expect(view).to_be_visible()

        # --- The session is lost while working (expired, new password): the next action shows the login screen
        log_in(page)
        page.context.clear_cookies()
        form = page.locator("#create-form")
        form.get_by_label("Path").fill("docs")
        form.get_by_label("Name", exact=True).fill("Docs")
        form.get_by_label("Target URL").fill("http://docs.intranet.local/")
        with run.watch.http_error(page, api, 401):
            form.get_by_role("button", name="Create redirect").click()
            expect(view).to_be_visible()
        expect(page.locator("#main")).to_be_hidden()
        log_in(page)
        expect_rows(page, FIXTURE_ROWS)
        page.context.close()


def settings_walk(run: Scenario, data_dir: Path) -> None:
    """The hostname of the links: defaults, an invalid name, a .local name announced, turned off."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        card = page.locator("#settings-card")
        enabled = card.get_by_label("Use a hostname in the links")
        hostname = card.get_by_label("Hostname", exact=True)
        save = card.get_by_role("button", name="Save")
        expect(card.get_by_role("heading", level=2)).to_have_text("Links")
        expect(enabled).to_be_checked()
        expect(hostname).to_have_value("redirect.lan")
        expect(card.locator("#settings-hostname-hint")).to_have_text(
            "Point this name at Redirect's address in your router or local DNS."
            " A name ending in .local needs no DNS: Redirect announces it on the network."
        )
        expect(card.locator("#settings-mdns")).to_be_hidden()
        expect(card.locator("#settings-public-base-url")).to_be_hidden()
        expect_rows(page, FIXTURE_ROWS)

        # --- An invalid name: the error is under the field, nothing changes
        hostname.fill("bad_name")
        with run.watch.http_error(page, base + "/api/settings", 422):
            save.click()
            expect(card.locator("#settings-hostname-error")).to_have_text(
                "Use a hostname such as redirect.lan: letters, digits and dashes, with dots between the parts."
            )
        expect(hostname).to_have_attribute("aria-invalid", "true")
        expect(hostname).to_be_focused()
        expect(card.locator("#settings-saved")).to_have_text("")
        expect_rows(page, FIXTURE_ROWS)
        run.shot(page, "29-hostname-error")

        # --- A .local name: announced on the network, and every link follows it
        hostname.fill("redirect.local")
        save.click()
        expect(card.locator("#settings-saved")).to_have_text("Saved.")
        expect(card.locator("#settings-hostname-error")).to_be_hidden()
        expect(card.locator("#settings-mdns")).to_have_text("Announced on the network as redirect.local.")
        local = "http://redirect.local/redirect/"
        expect(page.locator("#list .link").first).to_have_text(local + "compta/budget-2026")
        expect(page.locator("#create .prefix")).to_have_text(local)
        run.shot(page, "30-hostname-mdns")

        # --- Turned off: the links follow the address the browser used, the name is no longer announced
        enabled.uncheck()
        expect(hostname).to_be_disabled()
        save.click()
        expect(card.locator("#settings-saved")).to_have_text("Saved.")
        expect(card.locator("#settings-mdns")).to_be_hidden()
        expect(page.locator("#create .prefix")).to_have_text(base + "/redirect/")
        enabled.check()
        hostname.fill("redirect.lan")
        save.click()
        expect(page.locator("#create .prefix")).to_have_text(PREFIX)
        page.context.close()


def environment_walk(run: Scenario, data_dir: Path) -> None:
    """PUBLIC_BASE_URL and API_ENABLED set in the environment: the interface shows it and cannot change them."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir, public_base_url="https://go.example.org", api_enabled=True, local_only=True) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        expect(page.locator("#settings-public-base-url")).to_have_text(
            "PUBLIC_BASE_URL is set in the container's environment: links start with https://go.example.org,"
            " whatever the hostname."
        )
        expect(page.locator("#list .link").first).to_have_text("https://go.example.org/redirect/compta/budget-2026")
        toggle = page.get_by_label("Let scripts use the API")
        expect(toggle).to_be_checked()
        expect(toggle).to_be_disabled()
        expect(page.locator("#api-forced")).to_have_text("Turned on by API_ENABLED in the container's environment.")
        expect(page.locator("#key-state")).to_have_text("No API key yet.")
        local_only = page.get_by_label("Only answer on the local network")
        expect(local_only).to_be_checked()
        expect(local_only).to_be_disabled()
        expect(page.locator("#access-forced")).to_have_text("Turned on by LOCAL_ONLY in the container's environment.")
        run.shot(page, "31-environment")
        page.context.close()


# An address outside the local network: the scenario's server trusts X-Forwarded-For from 127.0.0.1
OUTSIDE = {"X-Forwarded-For": "203.0.113.9"}


def access_walk(run: Scenario, data_dir: Path) -> None:
    """Local only: an outside address gets the 403 page but follows links; turned off, it reaches the login."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        card = page.locator("#access-card")
        local_only = card.get_by_label("Only answer on the local network")
        expect(card.get_by_role("heading", level=2)).to_have_text("Access")
        expect(local_only).to_be_checked()
        expect(local_only).to_be_enabled()
        expect(card.locator(".access-help")).to_have_text(
            "From any other address, the interface and the API answer 403."
            " The links under /redirect/ and /links/ keep working from anywhere."
        )
        expect(card.locator("#access-off")).to_be_hidden()

        # --- From outside: the page says no, the links still work
        outside = run.open(base)
        outside.set_extra_http_headers(OUTSIDE)
        with run.watch.http_error(outside, base + "/", 403):
            response = outside.goto(base + "/")
        assert response is not None and response.status == 403
        expect(outside).to_have_title("Not available from here")
        expect(outside.locator(".standalone p")).to_have_text(
            "Redirect's interface only answers on the local network. The links it gives out work from anywhere."
        )
        expect(outside.get_by_role("link")).to_have_count(0)
        run.shot(outside, "41-denied", full_page=False, fonts=INTER)
        assert outside.request.get(base + "/redirect/wiki", headers=OUTSIDE, max_redirects=0).status == 302
        assert outside.request.get(base + "/api/list", headers=OUTSIDE).status == 403

        # --- Turned off: a warning here, and the outside reaches the login screen
        local_only.uncheck()
        expect(card.locator("#access-off")).to_have_text(
            "The interface and the API answer from any address: keep Redirect behind a firewall or a reverse proxy."
        )
        expect(local_only).to_be_focused()
        run.shot(page, "42-access-off")
        outside.goto(base + "/")
        expect(outside.locator("#login-view")).to_be_visible()
        outside.context.close()
        local_only.check()
        expect(card.locator("#access-off")).to_be_hidden()
        page.context.close()


def logs_walk(run: Scenario, data_dir: Path) -> None:
    """The Logs section: empty, then visits and a change, paged by 25, filtered."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        tabs = page.get_by_role("navigation", name="Sections")
        expect(tabs.get_by_role("link")).to_have_text(["Redirects", "NanoURL", "QR codes", "Logs"])
        expect(tabs.get_by_role("link", name="Redirects")).to_have_attribute("aria-current", "page")
        view = page.locator("#logs-view")

        # --- Nothing yet
        tabs.get_by_role("link", name="Logs").click()
        expect(page).to_have_url(base + "/#/logs")
        expect(view).to_be_visible()
        expect(page.locator("#main")).to_be_hidden()
        expect(tabs.get_by_role("link", name="Logs")).to_have_attribute("aria-current", "page")
        expect(view.get_by_role("heading", level=2)).to_have_text("Logs")
        expect(view.locator("#logs-empty")).to_have_text("Nothing logged yet.")
        expect(view.locator("#logs-table")).to_be_hidden()
        expect(view.locator("#logs-more")).to_be_hidden()

        # --- Visits from outside (24 of them, and one of a link that does not exist), then a change here
        wiki = FIXTURE_IDS["wiki"]
        for number in range(24):
            url = base + ("/redirect/wiki" if number % 2 else f"/links/{wiki}")
            assert page.request.get(url, max_redirects=0).status == 302
        assert page.request.get(base + "/redirect/compta/budget-2025", max_redirects=0).status == 404
        tabs.get_by_role("link", name="Redirects").click()
        expect(page.locator("#main")).to_be_visible()
        page.locator("#list .item").nth(2).get_by_role("button", name="Edit: Team wiki").click()
        page.locator("#edit-dialog").get_by_label("Target URL").fill("http://wiki.intranet.local/home")
        page.locator("#edit-dialog").get_by_role("button", name="Save changes").click()
        expect(page.locator("#status")).to_have_text("Redirect “Team wiki” saved.")

        # --- The log, newest first, 25 at a time
        tabs.get_by_role("link", name="Logs").click()
        rows = view.locator("#logs-rows tr")
        expect(rows).to_have_count(25)
        expect(view.locator("thead th")).to_have_text(
            ["Date (UTC)", "Event", "By", "Redirect", "Path", "Code", "Client", "Browser"]
        )
        expect(rows.nth(0).locator("td")).to_have_text(
            [
                "2026-01-15 09:30:00",
                "Changed",
                "Interface",
                f"Team wiki{wiki}",
                "wiki",
                "",
                "127.0.0.1",
                re.compile(".+"),
            ]
        )
        expect(rows.nth(1).locator("td").nth(4)).to_have_text("/redirect/compta/budget-2025")
        expect(rows.nth(1).locator("td").nth(5)).to_have_text("404")
        expect(rows.nth(1).locator("td").nth(5)).to_have_class("mono status-404")
        expect(rows.nth(2).locator("td").nth(1)).to_have_text("Visit")
        expect(rows.nth(2).locator("td").nth(2)).to_have_text("Link")
        more = view.get_by_role("button", name="Load more")
        expect(more).to_be_visible()
        expect(view.locator("#logs-empty")).to_be_hidden()
        run.shot(page, "32-logs")
        more.click()
        expect(rows).to_have_count(26)
        expect(more).to_be_hidden()

        # --- Only the changes
        view.get_by_label("Show").select_option("changes")
        expect(rows).to_have_count(1)
        expect(rows.nth(0).locator("td").nth(1)).to_have_text("Changed")
        run.shot(page, "33-logs-changes")
        page.context.close()


def nano_walk(run: Scenario, data_dir: Path) -> None:
    """NanoURL: empty, created (copied, followed), a name already used, changed, deleted."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        page.get_by_role("navigation", name="Sections").get_by_role("link", name="NanoURL").click()
        view = page.locator("#nano-view")
        items = view.locator("#nano-list .item")
        status = view.locator("#nano-status")

        # --- Nothing yet
        expect(view).to_be_visible()
        expect(page.locator("#main")).to_be_hidden()
        expect(view.get_by_role("heading", level=2, name="NanoURLs")).to_be_visible()
        expect(view.locator("#nano-empty p")).to_have_text(
            ["No NanoURL yet", "Give a name and a target in the form: Redirect makes the short link."]
        )
        form = view.locator("#nano-form")
        name, target = form.get_by_label("Name", exact=True), form.get_by_label("Target URL")
        submit = form.get_by_role("button", name="Create NanoURL")
        run.shot(page, "34-nano-empty", fonts=FORM_FONTS)

        # --- The link address: Redirect's IP by default, which works without any DNS; the hostname on demand
        expect(form.get_by_label("IP address")).to_be_checked()
        expect(form.locator("[data-link-example]")).to_have_text(
            [
                f"Works without any DNS: {IP_BASE}/links/<id>",
                f"Needs a DNS entry or mDNS to reach people: {LINKS_BASE}/links/<id>",
            ]
        )

        # --- Created: its short link comes first, ready to copy
        name.fill("Team lunch")
        target.fill("http://wiki.intranet.local/lunch")
        submit.click()
        short = f"{IP_BASE}/links/New00001"
        expect(status).to_have_text(f"NanoURL “Team lunch” created: {short}")
        expect(items).to_have_count(1)
        expect(items.locator(".link")).to_have_text(short)
        expect(items.locator(".item-name")).to_have_text("Team lunch")
        expect(items.locator(".target-url")).to_have_text("http://wiki.intranet.local/lunch")
        expect(items.locator(".item-short")).to_be_hidden()
        expect(view.locator("#nano-count")).to_have_text("1 NanoURL")
        copy = items.get_by_role("button", name="Copy link: Team lunch")
        expect(copy).to_be_focused()
        run.shot(page, "35-nano-created")
        copy.click()
        expect(copy).to_have_text("Copied")
        assert page.evaluate("() => navigator.clipboard.readText()") == short
        page.clock.run_for(2000)
        expect(copy).to_have_text("Copy link")
        with page.expect_popup() as popup_info:
            items.get_by_role("link", name=short).click()
        landed = popup_info.value
        expect(landed).to_have_url("http://wiki.intranet.local/lunch")
        landed.close()

        # --- The same name again: its path is taken
        name.fill("Team  lunch!")
        target.fill("http://wiki.intranet.local/other")
        with run.watch.http_error(page, base + "/api/nanourls", 409):
            submit.click()
            expect(view.locator("#nano-name-error")).to_have_text("This name is already used in this section.")
        expect(name).to_be_focused()
        run.shot(page, "36-nano-name-taken")

        # --- Not a redirect of the Redirects list
        page.get_by_role("link", name="Redirects").click()
        expect_rows(page, FIXTURE_ROWS)
        page.get_by_role("link", name="NanoURL").click()

        # --- Changed: the short link stays, the target moves
        items.get_by_role("button", name="Edit: Team lunch").click()
        dialog = page.locator("#nano-dialog")
        expect(dialog.get_by_role("heading", level=2)).to_have_text("Edit NanoURL")
        expect(dialog.get_by_label("Name", exact=True)).to_have_value("Team lunch")
        edit_target = dialog.get_by_label("Target URL")
        expect(edit_target).to_be_focused()
        expect(dialog.get_by_label("IP address")).to_be_checked()
        edit_target.fill("http://wiki.intranet.local/friday")
        dialog.get_by_role("button", name="Save changes").click()
        expect(status).to_have_text("Redirect “Team lunch” saved.")
        expect(items.locator(".link")).to_have_text(short)

        # --- On the hostname instead: the same short link, written with redirect.lan
        items.get_by_role("button", name="Edit: Team lunch").click()
        dialog.get_by_label("Hostname").check()
        dialog.get_by_role("button", name="Save changes").click()
        expect(items.locator(".link")).to_have_text(f"{LINKS_BASE}/links/New00001")
        items.get_by_role("button", name="Edit: Team lunch").click()
        expect(dialog.get_by_label("Hostname")).to_be_checked()
        dialog.get_by_label("IP address").check()
        dialog.get_by_role("button", name="Save changes").click()
        expect(items.locator(".link")).to_have_text(short)
        expect(items.locator(".target-url")).to_have_text("http://wiki.intranet.local/friday")

        # --- Deleted
        items.get_by_role("button", name="Delete: Team lunch").click()
        expect(page.locator("#delete-text")).to_have_text(
            f"The link {short} will stop working for everyone who has it."
        )
        page.locator("#delete-dialog").get_by_role("button", name="Delete").click()
        expect(status).to_have_text("Redirect “Team lunch” deleted.")
        expect(view.locator("#nano-empty")).to_be_visible()
        expect(page.locator("#nano-title")).to_be_focused()
        page.context.close()


QR_LOGO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<rect width="10" height="10" rx="2" fill="#c2185b"/><circle cx="5" cy="5" r="3" fill="#ffffff"/></svg>'
)


def preview_svg(page: Page) -> str:
    return page.evaluate(
        "() => decodeURIComponent(document.querySelector('#qr-preview').src.split(',').slice(1).join(','))"
    )


def set_range(page: Page, selector: str, value: int) -> None:
    """Ranges cannot be filled: set the value, then send the change the page listens to."""
    page.locator(selector).evaluate(
        "(input, value) => { input.value = value; input.dispatchEvent(new Event('change')) }", str(value)
    )


def qr_walk(run: Scenario, data_dir: Path) -> None:
    """QR codes: the editor and its preview, a full design with a logo, the SVG, a contrast error, delete."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    logo = data_dir / "logo.svg"
    logo.write_text(QR_LOGO, encoding="utf-8")
    with app_server(data_dir) as base:
        page = run.open(base)
        page.goto(base + "/")
        log_in(page)
        page.get_by_role("navigation", name="Sections").get_by_role("link", name="QR codes").click()
        view = page.locator("#qr-view")
        form = view.locator("#qr-form")
        cards = view.locator("#qr-list .qr-card")
        status = view.locator("#qr-status")
        preview = view.locator("#qr-preview")

        # --- Nothing yet: the editor shows a plain QR code
        expect(view.get_by_role("heading", level=2, name="QR codes")).to_be_visible()
        expect(view.locator("#qr-empty p")).to_have_text(
            ["No QR code yet", "Give a name and a target, choose a look, then download the SVG."]
        )
        expect(view.locator("#qr-form-title")).to_have_text("New QR code")
        expect(form.locator("legend")).to_have_text(["Link address", "Look"])
        expect(preview).to_have_attribute("src", re.compile(r"^data:image/svg\+xml"))
        expect(preview).to_have_attribute("alt", "Preview of the QR code")
        expect(view.locator("#qr-logo-size-value")).to_have_text("22 %")
        expect(view.locator("#qr-margin-value")).to_have_text("4 modules")
        expect(view.get_by_role("button", name="Remove the logo")).to_be_hidden()
        expect(view.locator("#qr-logo-size")).to_be_disabled()
        expect(view.get_by_role("button", name="Cancel")).to_be_hidden()
        expect(form.get_by_label("IP address")).to_be_checked()
        assert 'fill="#1b1f24"' in preview_svg(page)
        run.shot(page, "37-qr-empty", fonts=FORM_FONTS)

        # --- A full design: colors, dots, round corners, a logo, a thinner margin
        form.get_by_label("Name", exact=True).fill("Lunch menu")
        form.get_by_label("Target URL").fill("http://wiki.intranet.local/menu")
        form.get_by_label("Modules").fill("#0b3d91")
        form.get_by_label("Corners").fill("#c2185b")
        form.get_by_label("Module shape").select_option("dots")
        form.get_by_label("Corner shape").select_option("circle")
        form.get_by_label("Logo in the center").set_input_files(logo)
        expect(view.get_by_role("button", name="Remove the logo")).to_be_visible()
        expect(view.locator("#qr-logo-size")).to_be_enabled()
        set_range(page, "#qr-margin", 2)
        expect(view.locator("#qr-margin-value")).to_have_text("2 modules")
        page.wait_for_function(
            "() => decodeURIComponent(document.querySelector('#qr-preview').src).includes('<image href=\"data:image/svg+xml;base64,')"
        )
        svg = preview_svg(page)
        assert "<circle " in svg and 'fill="#0b3d91"' in svg and 'fill="#c2185b"' in svg
        run.shot(page, "38-qr-designed", fonts=FORM_FONTS)

        # --- Created: the card, the SVG file, the link it carries
        form.get_by_role("button", name="Create QR code").click()
        expect(status).to_have_text("QR code “Lunch menu” created.")
        expect(cards).to_have_count(1)
        card = cards.nth(0)
        short = f"{IP_BASE}/links/New00001"
        expect(card.locator(".item-name")).to_have_text("Lunch menu")
        expect(card.locator(".link")).to_have_text(short)
        expect(card.locator(".qr-image")).to_have_attribute("alt", "QR code of Lunch menu")
        page.wait_for_function("() => document.querySelector('.qr-card .qr-image').naturalWidth > 0")
        download = card.get_by_role("link", name="Download SVG: Lunch menu")
        expect(download).to_be_focused()
        expect(card.locator(".item-actions > *")).to_have_text(["Download SVG", "Edit", "Delete"])
        expect(card.locator(".target-label")).to_have_text("Target")
        expect(view.locator("#qr-form-title")).to_have_text("New QR code")
        expect(form.get_by_label("Name", exact=True)).to_have_value("")
        expect(view.locator("#qr-count")).to_have_text("1 QR code")
        run.shot(page, "39-qr-created")
        with page.expect_download() as download_info:
            download.click()
        saved = download_info.value
        assert saved.suggested_filename == "Lunch-menu.svg"
        content = Path(saved.path()).read_text(encoding="utf-8")
        assert content.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and 'fill="#c2185b"' in content
        with page.expect_popup() as popup_info:
            card.get_by_role("link", name=short).click()
        landed = popup_info.value
        expect(landed).to_have_url("http://wiki.intranet.local/menu")
        landed.close()

        # --- Edit: colors too close are refused before anything is saved, then a new corner shape
        card.get_by_role("button", name="Edit: Lunch menu").click()
        expect(view.locator("#qr-form-title")).to_have_text("Edit QR code")
        expect(form.get_by_label("Name", exact=True)).to_have_value("Lunch menu")
        expect(form.get_by_label("Name", exact=True)).to_be_focused()
        expect(form.get_by_label("Modules")).to_have_value("#0b3d91")
        expect(view.get_by_role("button", name="Cancel")).to_be_visible()
        with run.watch.http_error(page, base + "/api/qrcodes/preview", 422):
            form.get_by_label("Modules").fill("#eeeeee")
            expect(view.locator("#qr-design-error")).to_have_text(
                "The colors are too close to the background: scanners could not read the QR code."
            )
        run.shot(page, "40-qr-contrast")
        form.get_by_label("Modules").fill("#0b3d91")
        expect(view.locator("#qr-design-error")).to_be_hidden()
        form.get_by_label("Corner shape").select_option("rounded")
        form.get_by_role("button", name="Save changes").click()
        expect(status).to_have_text("Redirect “Lunch menu” saved.")
        expect(view.locator("#qr-form-title")).to_have_text("New QR code")
        served = page.request.get(base + f"/api/qrcodes/{id_of(page, base, 'QRcodes/Lunch-menu')}/svg").text()
        assert "<circle " in served and " 0 0 1 " in served  # dots, and rounded corners (arcs)

        # --- Deleted
        card.get_by_role("button", name="Delete: Lunch menu").click()
        expect(page.locator("#delete-text")).to_have_text(
            f"The link {short} will stop working for everyone who has it."
        )
        page.locator("#delete-dialog").get_by_role("button", name="Delete").click()
        expect(status).to_have_text("Redirect “Lunch menu” deleted.")
        expect(view.locator("#qr-empty")).to_be_visible()
        page.context.close()


def headless_walk(run: Scenario, data_dir: Path) -> None:
    """HEADLESS: the page says the interface is off; the API answers the key printed at startup."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir, headless=True) as base:
        page = run.open(base)
        with run.watch.http_error(page, base + "/", 404):
            response = page.goto(base + "/")
        assert response is not None and response.status == 404
        expect(page).to_have_title("Interface turned off")
        expect(page.locator(".standalone p")).to_have_text(
            "This Redirect runs without its interface (HEADLESS is set): its redirects are managed through the API."
        )
        expect(page.get_by_role("link")).to_have_count(0)
        run.shot(page, "43-headless", full_page=False, fonts=INTER)
        # The first key is the first of KEY_BODIES; the links keep working
        headers = {"Authorization": f"Bearer {FIRST_KEY}"}
        assert page.request.get(base + "/api/list", headers=headers).status == 200
        assert page.request.get(base + "/redirect/wiki", max_redirects=0).status == 302
        page.context.close()


def open_mode(run: Scenario, data_dir: Path) -> None:
    """No admin password: no login, and a red warning that stays on screen."""
    shutil.copyfile(FIXTURE_DB, data_dir / "redirect.db")
    with app_server(data_dir, password=None) as base:
        page = run.open(base)
        page.goto(base + "/")
        expect(page.locator("#main")).to_be_visible()
        expect(page.locator("#login-view")).to_be_hidden()
        warning = page.locator("#open-warning p")
        expect(warning).to_have_text(
            "No admin password is set: anyone who can reach this page can change or delete every redirect."
            " Set ADMIN_PASSWORD in the container's environment to require a login."
        )
        expect(warning).to_have_attribute("role", "alert")
        expect(page.locator("#logout")).to_be_hidden()
        expect(page.locator("#key-card")).to_be_hidden()
        expect_rows(page, FIXTURE_ROWS)
        run.shot(page, "28-open-mode")
        page.context.close()


def main(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    expect.set_options(timeout=TIMEOUT_MS)
    watch = Watch()
    try:
        with target_server() as target_port, sync_playwright() as playwright:
            browser = golden.launch(
                playwright, [f"--host-resolver-rules=MAP *.{TARGET_DOMAIN} 127.0.0.1:{target_port}"]
            )
            for suffix, viewport in (DESKTOP, MOBILE):
                print(f"viewport {viewport['width']}x{viewport['height']}", flush=True)
                run = Scenario(browser, out_dir, suffix, viewport, watch)
                with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as seeded:
                    empty_list(run, Path(empty))
                    main_walk(run, Path(seeded))
                for walk in (
                    auth_walk,
                    settings_walk,
                    environment_walk,
                    access_walk,
                    logs_walk,
                    nano_walk,
                    qr_walk,
                    headless_walk,
                    open_mode,
                ):
                    with tempfile.TemporaryDirectory() as data:
                        walk(run, Path(data))
            browser.close()
    except Exception:
        traceback.print_exc()
        watch.errors.append("the scenario stopped on the error above")
    expected = {f"{name}{suffix}.png" for name in SHOTS for suffix, _ in (DESKTOP, MOBILE)}
    produced = {path.name for path in out_dir.glob("*.png")}
    if produced != expected:
        watch.errors.append(
            f"captures missing: {sorted(expected - produced)}, unexpected: {sorted(produced - expected)}"
        )
    for error in watch.errors:
        print(error)
    return 1 if watch.errors else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))

"""End-to-end scenario on the real app + GUI goldens. Usage: screenshots.py OUT_DIR

Starts the real app in-process on a free port (a copy of fixtures/redirect.db, PUBLIC_BASE_URL fixed so the
links on screen are stable) and a small local target server, then walks every screen and state of the UI in
headless Chromium, at two viewports. Fails on any page error, unexpected console error, unexpected request
or missing element. Every capture goes through golden.py.
"""

import html
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

from redirect.config import Settings  # noqa: E402
from redirect.main import create_app  # noqa: E402

FIXTURE_DB = HERE / "fixtures" / "redirect.db"
PUBLIC_BASE_URL = "http://redirect.local:8080"
PREFIX = f"{PUBLIC_BASE_URL}/redirect/"
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
def app_server(data_dir: Path) -> Iterator[str]:
    """The real app, in this process, on a free port. Backend clock frozen at the golden time."""
    settings = Settings(data_dir=data_dir, public_base_url=PUBLIC_BASE_URL, log_level="WARNING")
    app = create_app(settings, clock=lambda: golden.GOLDEN_TIME)
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
            elif origin == PUBLIC_BASE_URL:
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
    for index, (name, slug, _) in enumerate(rows):
        item = items.nth(index)
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
            ("redirect.local", FIXTURE_ROWS),
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
        slug, name, target = form.get_by_label("Path"), form.get_by_label("Name"), form.get_by_label("Target URL")
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
        with run.watch.http_error(page, base + "/api/redirects/4", 422):
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
        lan.goto(PUBLIC_BASE_URL + "/")
        assert lan.evaluate("() => window.isSecureContext") is False
        lan_copy = lan.locator("#list .item").nth(2).get_by_role("button", name="Copy link: Team wiki")
        lan_copy.click()
        expect(lan_copy).to_have_text("Copied")
        lan.close()
        page.bring_to_front()
        page.reload()
        assert page.evaluate("() => navigator.clipboard.readText()") == PREFIX + "wiki"

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
        assert page.request.delete(base + "/api/redirects/3").status == 204
        with run.watch.http_error(page, base + "/api/redirects/3", 404):
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
        assert page.request.delete(base + "/api/redirects/2").status == 204
        # Budget 2028 goes too (id 5: Budget 2027, id 4, was deleted above), leaving a single entry
        assert page.request.delete(base + "/api/redirects/5").status == 204
        with run.watch.http_error(page, base + "/api/redirects/2", 404):
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

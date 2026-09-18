"""Structural guards: no external resource in the front end, no forbidden import, every UI text in strings.json,
test dependencies aligned with the app's lock."""

import ast
import json
import re
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "redirect"
STATIC = APP / "static"
STRINGS = json.loads((STATIC / "strings.json").read_text(encoding="utf-8"))

# The only absolute URL allowed in static/: the SVG namespace, an identifier that is never fetched
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
# Third-party packages the app may import: its runtime dependencies (pyproject.toml) and what they bring
ALLOWED_THIRD_PARTY = {"fastapi", "pydantic", "pydantic_core", "starlette", "uvicorn"}
# Redirect never fetches, proxies or runs anything: no HTTP client, no raw socket, no subprocess
FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "subprocess",
    "pickle",
}


# Files the browser loads as text. The rest of static/ is fonts/: woff2 files and their license texts.
TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".svg"}


def static_files() -> list[Path]:
    return sorted(p for p in STATIC.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)


def app_modules() -> list[Path]:
    return sorted(APP.glob("*.py"))


# --- No external resource ------------------------------------------------------------------------------


def test_static_holds_only_known_kinds_of_files():
    others = {
        p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file() and p.suffix not in TEXT_SUFFIXES
    }
    assert {name for name in others if not re.fullmatch(r"fonts/[\w-]+\.woff2|fonts/OFL-\w+\.txt", name)} == set()


@pytest.mark.parametrize("path", static_files(), ids=lambda p: p.name)
def test_no_external_url_in_static_files(path):
    text = path.read_text(encoding="utf-8")
    urls = set(re.findall(r"""https?://[^\s"'<>)`]+""", text))
    # "http:// or https://" in the error message is text, not a URL
    urls = {url for url in urls if re.match(r"https?://[\w\[]", url)}
    assert urls <= ALLOWED_URLS, f"external URL in {path.name}: {sorted(urls - ALLOWED_URLS)}"


@pytest.mark.parametrize("path", static_files(), ids=lambda p: p.name)
def test_no_protocol_relative_or_imported_resource(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"""(?:src|href|action)\s*=\s*["']//""", text)
    assert not re.search(r"""url\(\s*["']?(?://|https?:)""", text)
    assert "@import" not in text


def test_web_fonts_are_self_hosted_and_all_used():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    referenced = set(re.findall(r"url\('(fonts/[^']+)'\)", css))
    shipped = {p.relative_to(STATIC).as_posix() for p in (STATIC / "fonts").glob("*.woff2")}
    assert referenced == shipped
    assert len(shipped) == 4
    # every family comes with its license
    assert {p.name for p in (STATIC / "fonts").glob("OFL-*.txt")} == {"OFL-Inter.txt", "OFL-JetBrainsMono.txt"}


def test_the_svg_namespace_is_the_only_allowed_url_and_it_is_used():
    assert 'xmlns="http://www.w3.org/2000/svg"' in (STATIC / "favicon.svg").read_text(encoding="utf-8")


# --- Imports -------------------------------------------------------------------------------------------


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


@pytest.mark.parametrize("path", app_modules(), ids=lambda p: p.name)
def test_no_forbidden_import_in_the_app(path):
    modules = imported_modules(path)
    forbidden = {m for m in modules if m in FORBIDDEN_MODULES or m.split(".")[0] in FORBIDDEN_MODULES}
    assert not forbidden, f"{path.name} imports {sorted(forbidden)}"


@pytest.mark.parametrize("path", app_modules(), ids=lambda p: p.name)
def test_the_app_imports_only_the_stdlib_and_its_dependencies(path):
    roots = {m.split(".")[0] for m in imported_modules(path)}
    unknown = roots - set(sys.stdlib_module_names) - ALLOWED_THIRD_PARTY
    assert not unknown, f"{path.name} imports {sorted(unknown)}, not a declared dependency"


def test_the_import_guard_sees_imports():
    assert {"fastapi", "sqlite3", "urllib.parse", "urllib.parse.urlsplit"} <= set().union(
        *(imported_modules(p) for p in app_modules())
    )


# --- Texts ---------------------------------------------------------------------------------------------


class _TextCollector(HTMLParser):
    """Visible text and text-bearing attributes of an HTML file."""

    TEXT_ATTRIBUTES = {"alt", "title", "placeholder", "aria-label", "value", "label"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.attributes: list[tuple[str, str, str]] = []
        self.keys: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("data-i18n", "data-i18n-placeholder") and value:
                self.keys.add(value)
            elif name in self.TEXT_ATTRIBUTES and value:
                self.attributes.append((tag, name, value))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.texts.append(data.strip())


def collect(name: str) -> _TextCollector:
    parser = _TextCollector()
    parser.feed((STATIC / name).read_text(encoding="utf-8"))
    return parser


def test_index_html_has_no_text_of_its_own():
    page = collect("index.html")
    assert page.texts == []
    assert page.attributes == []


def test_index_html_keys_exist():
    keys = collect("index.html").keys
    assert keys, "no data-i18n attribute found"
    assert keys <= STRINGS.keys(), sorted(keys - STRINGS.keys())


def test_404_html_text_is_only_placeholders():
    page = collect("404.html")
    placeholders = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
    assert page.attributes == []
    for text in page.texts:
        assert placeholders.fullmatch(text), f"literal text in 404.html: {text!r}"
        assert placeholders.fullmatch(text).group(1) in STRINGS


def js_source() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def test_app_js_keys_exist():
    keys = set(re.findall(r"""\bt\(\s*'([\w.]+)'""", js_source()))
    assert keys, "no t('...') call found"
    assert keys <= STRINGS.keys(), sorted(keys - STRINGS.keys())


# Literals of app.js that read like prose but are developer messages, never shown in the page
JS_DEVELOPER_TEXTS = {"Missing string: ${key}"}


def test_app_js_has_no_user_facing_literal():
    literals = re.findall(r"""'([^'\n]*)'|`([^`\n]*)`|"([^"\n]*)\"""", js_source())
    prose = re.compile(r"[A-Z][a-z]+(?: [a-z]+)+")
    found = {text for groups in literals for text in groups if text and prose.match(text)}
    assert found == JS_DEVELOPER_TEXTS


def python_keys() -> set[str]:
    source = "\n".join(p.read_text(encoding="utf-8") for p in app_modules())
    keys = set(re.findall(r"""\bt\(\s*["']([\w.]+)["']""", source))
    keys |= {f"errors.{kind}" for kind in re.findall(r"""\b_error\(\s*["'](\w+)["']""", source)}
    return keys


def test_backend_keys_exist():
    keys = python_keys()
    assert keys <= STRINGS.keys(), sorted(keys - STRINGS.keys())


def test_every_text_is_used():
    used = collect("index.html").keys | collect("404.html").keys | python_keys()
    used |= set(re.findall(r"""\bt\(\s*'([\w.]+)'""", js_source()))
    used |= {m.strip() for m in re.findall(r"\{\{([^}]+)\}\}", (STATIC / "404.html").read_text(encoding="utf-8"))}
    assert set(STRINGS) - used == set()


def test_strings_json_is_a_flat_object_of_non_empty_strings():
    assert all(isinstance(value, str) and value.strip() for value in STRINGS.values())


def test_api_error_messages_come_from_strings_json():
    """Every JSON error the backend builds takes its message from t(), never from a literal."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_error"
    ]
    assert calls
    for call in calls:
        for arg in call.args[1:]:
            assert not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)), ast.unparse(call)


# --- Dependencies --------------------------------------------------------------------------------------


def pins(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            found[re.sub(r"[-_.]+", "-", name).lower()] = version.strip()
    return found


def app_lock() -> dict[str, str]:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if p["name"] != "redirect"}


def test_tests_run_on_the_versions_the_app_ships_with():
    locked = app_lock()
    assert locked, "no package in uv.lock"
    for path in (REPO / "test/redirect/requirements-test.txt", REPO / "test/ci/requirements.lock"):
        found = pins(path)
        mismatched = {
            name: (version, found.get(name)) for name, version in locked.items() if found.get(name) != version
        }
        assert not mismatched, f"{path.relative_to(REPO)} differs from uv.lock (uv.lock, file): {mismatched}"


def test_runtime_dependencies_are_pinned():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]
    assert all(re.fullmatch(r"[\w-]+==[\w.]+", dep) for dep in dependencies), dependencies
    locked = app_lock()
    assert {dep.split("==")[0]: dep.split("==")[1] for dep in dependencies}.items() <= locked.items()

"""Unit tests: slug, name and target validation, public link construction, configuration, texts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from redirect import strings
from redirect.config import Settings
from redirect.main import create_app
from redirect.models import (
    NAME_MAX_LENGTH,
    SLUG_MAX_LENGTH,
    TARGET_MAX_LENGTH,
    RedirectIn,
    is_valid_slug,
    public_url,
    validate_name,
    validate_slug,
    validate_target,
)

SLUG_INVALID = "Use only letters, digits, dots, dashes and underscores, with / between segments."
SLUG_EMPTY_SEGMENT = "The path cannot start or end with /, or contain //."
SLUG_DOT_SEGMENT = "A path segment cannot be . or .."
TARGET_INVALID = "Enter a full URL that starts with http:// or https://."


def rejected(validator, value: str) -> str:
    """The message a validator raises for `value` (fails the test if it accepts it)."""
    with pytest.raises(PydanticCustomError) as info:
        validator(value)
    return str(info.value)


# --- Slug: one passing and one failing case per rule ---------------------------------------------------


@pytest.mark.parametrize(
    "slug", ["budget", "compta/budget-2026", "local-api/Test1", "a.b_c-D9", "v1.2/x..y", "a/b/c/d/e"]
)
def test_slug_accepts_segments_of_allowed_characters(slug):
    assert validate_slug(slug) == slug
    assert is_valid_slug(slug)


@pytest.mark.parametrize("slug", ["a b", "budget!", "compta/été", "a/b?c", "a#b", "a%20b", "a\\b", "a:b", "~user/x/+"])
def test_slug_rejects_characters_outside_the_segment_alphabet(slug):
    assert rejected(validate_slug, slug) == SLUG_INVALID
    assert not is_valid_slug(slug)


@pytest.mark.parametrize("slug", ["/budget", "budget/", "compta//budget", "/", "//"])
def test_slug_rejects_empty_segments(slug):
    assert rejected(validate_slug, slug) == SLUG_EMPTY_SEGMENT


@pytest.mark.parametrize("slug", ["..", ".", "a/../b", "a/./b", "compta/..", "../etc/passwd"])
def test_slug_rejects_dot_segments(slug):
    assert rejected(validate_slug, slug) == SLUG_DOT_SEGMENT


def test_slug_allows_dots_inside_a_segment():
    assert validate_slug("a..b/...x/.hidden") == "a..b/...x/.hidden"


@pytest.mark.parametrize("slug", ["", "   "])
def test_slug_is_required(slug):
    assert rejected(validate_slug, slug) == "Enter a path."


def test_slug_length_is_bounded():
    longest = "a" * SLUG_MAX_LENGTH
    assert SLUG_MAX_LENGTH == 255
    assert validate_slug(longest) == longest
    assert rejected(validate_slug, longest + "b") == "The path must be at most 255 characters."


def test_slug_is_case_sensitive_and_kept_as_typed():
    assert validate_slug("Local-API/Test1") == "Local-API/Test1"
    assert validate_slug("local-api/test1") == "local-api/test1"


def test_slug_is_stripped_but_not_otherwise_changed():
    assert validate_slug("  compta/budget-2026 \t") == "compta/budget-2026"
    # is_valid_slug is for incoming paths: surrounding spaces are not part of a stored slug
    assert not is_valid_slug(" compta/budget-2026")


def test_slug_rules_are_checked_in_a_fixed_order():
    assert rejected(validate_slug, "a" * 300 + "/") == "The path must be at most 255 characters."
    assert rejected(validate_slug, "/../bad slug") == SLUG_EMPTY_SEGMENT
    assert rejected(validate_slug, "../bad slug") == SLUG_DOT_SEGMENT


# --- Name ----------------------------------------------------------------------------------------------


def test_name_is_stripped():
    assert validate_name("  Budget 2026  ") == "Budget 2026"


@pytest.mark.parametrize("name", ["", " \t "])
def test_name_is_required(name):
    assert rejected(validate_name, name) == "Enter a name."


def test_name_length_is_bounded():
    assert validate_name("n" * NAME_MAX_LENGTH) == "n" * NAME_MAX_LENGTH
    assert rejected(validate_name, "n" * (NAME_MAX_LENGTH + 1)) == "The name must be at most 100 characters."


@pytest.mark.parametrize("name", ["tab\there", "new\nline", "bell\x07", "del\x7f"])
def test_name_rejects_control_characters(name):
    assert rejected(validate_name, name) == "The name cannot contain control characters."


def test_name_accepts_unicode():
    assert validate_name("Budget “2026” – été ✓") == "Budget “2026” – été ✓"


# --- Target --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://10.12.75.1/api/test1",
        "https://example.com",
        "https://example.com:8443/a/b?x=1&y=2#frag",
        "http://user:pass@files.intranet.local/Budget%202026.xlsx",
        "http://[::1]:8080/",
        "http://localhost",
    ],
)
def test_target_accepts_absolute_http_and_https_urls(target):
    assert validate_target(target) == target


def test_target_is_stripped():
    assert validate_target("  https://example.com/x  ") == "https://example.com/x"


@pytest.mark.parametrize(
    "target",
    [
        "ftp://files.intranet.local/budget.xlsx",
        "javascript:alert(1)",
        "data:text/html,<p>x</p>",
        "file:///etc/passwd",
        "mailto:someone@example.com",
        "files.intranet.local/budget.xlsx",
        "/relative/path",
        "//example.com/protocol-relative",
        "http://",
        "http:///path-without-host",
        "https://example.com:99999/",
        "https://example.com:port/",
        "http://exa mple.com/",
        "http://example.com/a b",
        "http://example.com/\x00",
    ],
)
def test_target_rejects_anything_but_an_absolute_http_url(target):
    assert rejected(validate_target, target) == TARGET_INVALID


@pytest.mark.parametrize("target", ["", "  "])
def test_target_is_required(target):
    assert rejected(validate_target, target) == "Enter a target URL."


def test_target_length_is_bounded():
    prefix = "https://example.com/"
    longest = prefix + "x" * (TARGET_MAX_LENGTH - len(prefix))
    assert validate_target(longest) == longest
    assert rejected(validate_target, longest + "x") == "The target URL must be at most 2048 characters."


def test_target_non_ascii_is_made_ascii_for_the_location_header():
    assert validate_target("https://bücher.example/ä?q=é#ü") == "https://xn--bcher-kva.example/%C3%A4?q=%C3%A9#%C3%BC"
    assert validate_target("http://intranet.local/Compta/Budget été.xlsx".replace(" ", "%20")) == (
        "http://intranet.local/Compta/Budget%20%C3%A9t%C3%A9.xlsx"
    )


def test_target_non_ascii_keeps_port_and_user_info():
    assert validate_target("http://usér@bücher.example:8080/") == "http://us%C3%A9r@xn--bcher-kva.example:8080/"


def test_target_length_is_checked_again_after_percent_encoding():
    prefix = "https://example.com/"
    # 1000 "é" fit before encoding (1020 characters) but not after (6000 characters)
    assert rejected(validate_target, prefix + "é" * 1000) == "The target URL must be at most 2048 characters."


def test_target_with_an_invalid_international_host_is_rejected():
    assert rejected(validate_target, "http://" + "ü" * 70 + ".example/") == TARGET_INVALID


# --- The request model ---------------------------------------------------------------------------------


def test_request_model_normalizes_every_field():
    body = RedirectIn(slug=" wiki ", name=" Team wiki ", target=" http://wiki.intranet.local/start ")
    assert body.model_dump() == {"slug": "wiki", "name": "Team wiki", "target": "http://wiki.intranet.local/start"}


def test_request_model_reports_one_error_per_field():
    with pytest.raises(ValidationError) as info:
        RedirectIn(slug="a b", name="", target="ftp://x")
    assert {(e["loc"], e["type"], e["msg"]) for e in info.value.errors()} == {
        (("slug",), "slug_invalid", SLUG_INVALID),
        (("name",), "name_required", "Enter a name."),
        (("target",), "target_invalid", TARGET_INVALID),
    }


# --- Public link ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "slug", "expected"),
    [
        ("http://redirect.local:8080", "compta/budget-2026", "http://redirect.local:8080/redirect/compta/budget-2026"),
        ("http://redirect.local:8080/", "wiki", "http://redirect.local:8080/redirect/wiki"),
        (
            "https://links.example.com/tools",
            "local-api/Test1",
            "https://links.example.com/tools/redirect/local-api/Test1",
        ),
    ],
)
def test_public_url_joins_base_prefix_and_slug(base, slug, expected):
    assert public_url(base, slug) == expected


# --- Configuration -------------------------------------------------------------------------------------


def test_settings_defaults():
    settings = Settings.from_env({})
    assert settings == Settings(port=80, public_base_url=None, log_level="INFO")
    assert str(settings.db_path) == "/data/redirect.db"


def test_settings_from_environment():
    settings = Settings.from_env(
        {
            "PORT": " 9000 ",
            "DATA_DIR": "/srv/redirect",
            "PUBLIC_BASE_URL": " http://10.0.0.5:9000/ ",
            "LOG_LEVEL": "debug",
        }
    )
    assert settings.port == 9000
    assert str(settings.db_path) == "/srv/redirect/redirect.db"
    assert settings.public_base_url == "http://10.0.0.5:9000"
    assert settings.log_level == "DEBUG"


def test_settings_empty_public_base_url_means_unset():
    assert Settings.from_env({"PUBLIC_BASE_URL": "  "}).public_base_url is None


def test_settings_reject_a_non_integer_port():
    with pytest.raises(ValueError) as info:
        Settings.from_env({"PORT": "80a"})
    assert str(info.value) == "PORT must be an integer, got '80a'"


@pytest.mark.parametrize(("port", "message"), [("0", "got 0"), ("65536", "got 65536"), ("-80", "got -80")])
def test_settings_reject_a_port_out_of_range(port, message):
    with pytest.raises(ValueError) as info:
        Settings.from_env({"PORT": port})
    assert str(info.value) == f"PORT must be between 1 and 65535, {message}"


@pytest.mark.parametrize("level", ["DEBUG", "info", " Warning ", "ERROR", "critical"])
def test_settings_accept_the_log_levels_of_logging_and_uvicorn(level):
    assert Settings.from_env({"LOG_LEVEL": level}).log_level == level.strip().upper()


@pytest.mark.parametrize("level", ["verbose", "warn", "trace", "10"])
def test_settings_reject_an_unknown_log_level(level):
    with pytest.raises(ValueError) as info:
        Settings.from_env({"LOG_LEVEL": level})
    assert str(info.value) == f"LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL, got {level!r}"


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        (
            "LOG_LEVEL",
            "verbose",
            "redirect: LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL, got 'verbose'",
        ),
        ("PORT", "70000", "redirect: PORT must be between 1 and 65535, got 70000"),
        (
            "PUBLIC_BASE_URL",
            "files.lan",
            "redirect: PUBLIC_BASE_URL must be an http:// or https:// URL, got 'files.lan'",
        ),
    ],
)
def test_the_server_reports_a_bad_configuration_in_one_line(tmp_path, variable, value, message):
    env = {**os.environ, "DATA_DIR": str(tmp_path), variable: value}
    result = subprocess.run(
        [sys.executable, "-m", "redirect"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == message + "\n"
    assert not any(tmp_path.iterdir()), "nothing may be written before the configuration is valid"


def test_the_app_refuses_a_public_base_url_that_is_not_http(tmp_path):
    with pytest.raises(ValueError) as info:
        create_app(Settings(data_dir=tmp_path, public_base_url="ftp://files.lan"))
    assert str(info.value) == "PUBLIC_BASE_URL must be an http:// or https:// URL, got 'ftp://files.lan'"


# --- Texts ---------------------------------------------------------------------------------------------


def test_texts_fill_placeholders_once():
    assert strings.t("status.created", name="Budget {name}") == "Redirect “Budget {name}” created."
    assert strings.t("list.count.other", count="3") == "3 redirects"


def test_texts_keep_unknown_placeholders_and_fail_on_unknown_keys():
    assert strings.t("list.count.other") == "{count} redirects"
    with pytest.raises(KeyError):
        strings.t("no.such.key")


# --- Reserved sections --------------------------------------------------------------------------------------

RESERVED = "Paths starting with Api, QRcodes or NanoURL are reserved for Redirect's own sections."


@pytest.mark.parametrize("slug", ["Api", "api/x", "QRcodes/menu", "qrcodes", "NanoURL/a/b", "NANOURL/x"])
def test_reserved_sections_are_refused(slug):
    with pytest.raises(ValidationError) as info:
        RedirectIn(slug=slug, name="n", target="http://t/")
    assert info.value.errors()[0]["msg"] == RESERVED


@pytest.mark.parametrize("slug", ["apis", "my/Api", "QRcodes2", "nano", "x/NanoURL/y"])
def test_names_close_to_the_sections_are_allowed(slug):
    assert RedirectIn(slug=slug, name="n", target="http://t/").slug == slug


def test_the_reserved_check_comes_after_the_path_rules():
    with pytest.raises(ValidationError) as info:
        RedirectIn(slug="Api//x", name="n", target="http://t/")
    assert info.value.errors()[0]["msg"] == "The path cannot start or end with /, or contain //."

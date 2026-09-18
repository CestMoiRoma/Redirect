"""The prose linter (test/ci/check_prose.py): fails on any em or en dash in Markdown, passes otherwise, never
writes, and is wired into treefmt so that `just lint` runs it on every *.md file."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "test/ci/check_prose.py"
HINT = "No em or en dash in prose: use a comma, a colon, a full stop or two sentences."


def run(cwd: Path, *names: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *names], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )


def write(folder: Path, name: str, text: str) -> str:
    (folder / name).write_text(text, encoding="utf-8")
    return name


def test_plain_prose_passes(tmp_path):
    name = write(
        tmp_path,
        "clean.md",
        "# Title\n\nHyphens, a-b, minus -1, double -- and triple --- hyphens pass.\n\n- a list item\n\n---\n",
    )
    result = run(tmp_path, name)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize(
    ("dash", "what"),
    [
        ("—", "em dash (U+2014)"),
        ("–", "en dash (U+2013)"),
        ("&mdash;", "em dash (&mdash;)"),
        ("&ndash;", "en dash (&ndash;)"),
        ("&#8212;", "em dash (&#8212;)"),
        ("&#8211;", "en dash (&#8211;)"),
        ("&#x2014;", "em dash (&#x2014;)"),
        ("&#X2013;", "en dash (&#X2013;)"),
    ],
)
def test_a_dash_fails_with_its_position(tmp_path, dash, what):
    name = write(tmp_path, "dash.md", f"# Title\n\nOne thing{dash}then another.\n")
    result = run(tmp_path, name)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [f"dash.md:3:10: {what}", HINT]


def test_every_dash_of_every_file_is_reported(tmp_path):
    clean = write(tmp_path, "clean.md", "Nothing to see.\n")
    dashes = write(tmp_path, "dashes.md", "a—b–c\n\nd &mdash; e\n")
    result = run(tmp_path, clean, dashes)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "dashes.md:1:2: em dash (U+2014)",
        "dashes.md:1:4: en dash (U+2013)",
        "dashes.md:3:3: em dash (&mdash;)",
        HINT,
    ]


def test_the_checked_file_is_never_rewritten(tmp_path):
    name = write(tmp_path, "dash.md", "a — b\r\n")
    before = (tmp_path / name).read_bytes()
    assert run(tmp_path, name).returncode == 1
    assert (tmp_path / name).read_bytes() == before


def test_an_unreadable_file_fails(tmp_path):
    (tmp_path / "latin1.md").write_bytes("caf\xe9\n".encode("latin-1"))
    result = run(tmp_path, "latin1.md", "missing.md")
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert [line.split(": cannot read: ")[0] for line in lines[:-1]] == ["latin1.md", "missing.md"]
    assert lines[-1] == HINT


def test_treefmt_runs_it_on_every_markdown_file():
    config = tomllib.loads((REPO / "treefmt.toml").read_text(encoding="utf-8"))
    prose = config["formatter"]["prose"]
    assert (prose["command"], prose["options"], prose["includes"]) == (
        "python3",
        ["test/ci/check_prose.py"],
        ["*.md"],
    )
    assert "excludes" not in prose

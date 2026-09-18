#!/usr/bin/env python3
"""treefmt linter: prose files must not contain an em dash or an en dash, typed as a character or written as
an HTML entity. Split the sentence with a comma, a colon or a full stop instead. Read-only."""

import re
import sys

# U+2013 en dash, U+2014 em dash, and the entities that render as one of them
DASH = re.compile(r"[–—]|&(?:ndash|mdash|#8211|#8212|#x2013|#x2014);", re.IGNORECASE)
NAMES = {"–": "en dash", "—": "em dash", "ndash": "en dash", "mdash": "em dash"}
CODES = {"8211": "en dash", "8212": "em dash", "x2013": "en dash", "x2014": "em dash"}


def describe(match):
    text = match.group(0)
    if len(text) == 1:
        return f"{NAMES[text]} (U+{ord(text):04X})"
    body = text[1:-1].lower().lstrip("#")
    return f"{NAMES.get(body) or CODES[body]} ({text})"


def check(path):
    """Findings of one file, as "path:line:column: what" lines."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return [
        f"{path}:{number}:{match.start() + 1}: {describe(match)}"
        for number, line in enumerate(lines, start=1)
        for match in DASH.finditer(line)
    ]


def main(paths):
    findings = []
    for path in paths:
        try:
            findings += check(path)
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(f"{path}: cannot read: {exc}")
    for finding in findings:
        print(finding)
    if findings:
        print("No em or en dash in prose: use a comma, a colon, a full stop or two sentences.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

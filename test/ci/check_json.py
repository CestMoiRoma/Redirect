#!/usr/bin/env python3
"""treefmt linter: every given JSON file must parse as strict JSON (UTF-8, no NaN, no duplicate key). Read-only."""

import json
import sys


def no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def reject_constant(name):
    raise ValueError(f"invalid constant {name}")


def main(paths):
    failures = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig") as handle:
                json.load(handle, object_pairs_hook=no_duplicates, parse_constant=reject_constant)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            failures += 1
            print(f"{path}: invalid JSON: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

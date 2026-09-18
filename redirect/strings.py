"""User-facing texts. The single source is static/strings.json, shared with the front end."""

import json
import re
from functools import cache
from pathlib import Path
from typing import cast

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
STRINGS_PATH = Path(__file__).resolve().parent / "static" / "strings.json"


@cache
def _load() -> dict[str, str]:
    with STRINGS_PATH.open(encoding="utf-8") as handle:
        data: object = json.load(handle)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"{STRINGS_PATH} must be a flat object of strings")
    return cast(dict[str, str], data)


def t(key: str, **values: str) -> str:
    """Text for `key`, with `{name}` placeholders replaced. Unknown keys raise KeyError."""
    text = _load()[key]
    # One pass, so a value that itself contains "{...}" is never substituted again
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)

"""The data of fixtures/redirect.db, the database the end-to-end scenario starts from.

Regenerate it (in the CI image) after a schema migration:
    python test/redirect/seed.py test/redirect/fixtures/redirect.db
test_fixture_db.py fails while the committed file and this module disagree.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from redirect.db import Database

FIXTURE_TIME = datetime(2026, 1, 12, 8, 0, 0, tzinfo=UTC)

# (slug, name, target), in insertion order: ids 1, 2, 3
FIXTURE_REDIRECTS = (
    ("compta/budget-2026", "Budget 2026", "http://files.intranet.local/compta/Budget%202026.xlsx"),
    ("local-api/Test1", "Redirect api", "http://10.12.75.1/api/test1"),
    ("wiki", "Team wiki", "http://wiki.intranet.local/start"),
)


def write(path: Path) -> None:
    if path.exists():
        path.unlink()
    db = Database(path, clock=lambda: FIXTURE_TIME)
    db.migrate()
    for slug, name, target in FIXTURE_REDIRECTS:
        db.create(slug, name, target)


if __name__ == "__main__":
    write(Path(sys.argv[1]))

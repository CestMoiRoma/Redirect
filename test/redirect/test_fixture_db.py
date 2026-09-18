"""The scenario's fixture database and the storage layer: schema, migrations, errors."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from seed import FIXTURE_REDIRECTS
from support import FrozenClock

from redirect.db import MIGRATIONS, Database, DatabaseError, SlugTakenError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "redirect.db"


def test_fixture_db_is_up_to_date(tmp_path):
    copy = tmp_path / "redirect.db"
    shutil.copyfile(FIXTURE, copy)
    with sqlite3.connect(copy) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(MIGRATIONS), "fixtures/redirect.db is behind the schema: regenerate it with seed.py"
    rows = Database(copy).list_all()
    assert sorted((r.id, r.slug, r.name, r.target) for r in rows) == [
        (index, *entry) for index, entry in enumerate(FIXTURE_REDIRECTS, start=1)
    ]


def test_migrate_creates_the_data_dir_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "a" / "b" / "redirect.db", FrozenClock())
    db.migrate()
    db.create("wiki", "Wiki", "http://w/")
    db.migrate()
    assert [r.slug for r in db.list_all()] == ["wiki"]


def test_migrate_refuses_a_newer_schema(tmp_path):
    path = tmp_path / "redirect.db"
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(DatabaseError) as info:
        Database(path).migrate()
    assert str(info.value) == (
        f"{path} has schema version {len(MIGRATIONS) + 1}, newer than this release ({len(MIGRATIONS)})."
        " Run a newer Redirect or restore a backup."
    )


def test_migrate_reports_a_data_dir_that_cannot_be_created(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    path = blocker / "data" / "redirect.db"
    with pytest.raises(DatabaseError) as info:
        Database(path).migrate()
    assert str(info.value).startswith(f"Cannot open the database {path}: ")
    assert str(info.value).endswith(f" Check that DATA_DIR ({path.parent}) exists and is writable by this user.")


def test_storage_round_trip_with_the_injected_clock(tmp_path):
    clock = FrozenClock()
    db = Database(tmp_path / "redirect.db", clock)
    db.migrate()
    created = db.create("wiki", "Wiki", "http://w/")
    assert (created.created_at, created.updated_at) == ("2026-01-15T09:30:00Z", "2026-01-15T09:30:00Z")
    clock.advance(days=1)
    updated = db.update(created.id, "wiki", "Wiki", "http://w2/")
    assert updated is not None
    assert (updated.created_at, updated.updated_at) == ("2026-01-15T09:30:00Z", "2026-01-16T09:30:00Z")
    assert db.target_for("wiki") == "http://w2/"
    assert db.get(created.id) == updated
    assert db.delete(created.id) == updated
    assert db.get(created.id) is None
    assert db.update(created.id, "wiki", "Wiki", "http://w/") is None
    assert db.delete(created.id) is None


def test_storage_raises_slug_taken(tmp_path):
    db = Database(tmp_path / "redirect.db")
    db.migrate()
    db.create("wiki", "Wiki", "http://w/")
    other = db.create("docs", "Docs", "http://d/")
    with pytest.raises(SlugTakenError):
        db.create("wiki", "Wiki again", "http://w/")
    with pytest.raises(SlugTakenError):
        db.update(other.id, "wiki", "Docs", "http://d/")

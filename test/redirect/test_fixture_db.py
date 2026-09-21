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
    assert sorted((r.code, r.slug, r.name, r.target) for r in rows) == sorted(FIXTURE_REDIRECTS)


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
    updated = db.update(created.code, "wiki", "Wiki", "http://w2/")
    assert updated is not None
    assert (updated.created_at, updated.updated_at) == ("2026-01-15T09:30:00Z", "2026-01-16T09:30:00Z")
    assert db.target_for("wiki") == "http://w2/"
    assert db.get(created.code) == updated
    assert db.delete(created.code) == updated
    assert db.get(created.code) is None
    assert db.update(created.code, "wiki", "Wiki", "http://w/") is None
    assert db.delete(created.code) is None


def test_storage_raises_slug_taken(tmp_path):
    db = Database(tmp_path / "redirect.db")
    db.migrate()
    db.create("wiki", "Wiki", "http://w/")
    other = db.create("docs", "Docs", "http://d/")
    with pytest.raises(SlugTakenError):
        db.create("wiki", "Wiki again", "http://w/")
    with pytest.raises(SlugTakenError):
        db.update(other.code, "wiki", "Docs", "http://d/")


def test_new_ids_are_short_and_unambiguous():
    from redirect.db import CODE_ALPHABET, CODE_LENGTH, new_code

    codes = {new_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET) for code in codes)
    assert not set("0O1lI") & set(CODE_ALPHABET)


def test_a_taken_id_is_drawn_again(tmp_path):
    drawn = iter(["AAAAAAAA", "AAAAAAAA", "BBBBBBBB"])
    db = Database(tmp_path / "redirect.db", FrozenClock(), make_code=lambda: next(drawn))
    db.migrate()
    assert db.create("one", "One", "http://1/").code == "AAAAAAAA"
    assert db.create("two", "Two", "http://2/").code == "BBBBBBBB"


def test_the_migration_gives_existing_redirects_an_id(tmp_path):
    """A database of the previous schema (version 2) keeps its redirects and gets an id for each."""
    path = tmp_path / "redirect.db"
    with sqlite3.connect(path) as conn:
        for statement in MIGRATIONS[:2]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO redirects (slug, name, target, created_at, updated_at) VALUES ('wiki', 'Wiki', 'http://w/', 'x', 'x')"
        )
    db = Database(path)
    db.migrate()
    [row] = db.list_all()
    assert row.slug == "wiki"
    assert len(row.code) == 8
    assert db.target_for_code(row.code) == "http://w/"

"""SQLite storage: one connection per operation, schema migrated at startup."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

Clock = Callable[[], datetime]

# Applied in order; PRAGMA user_version holds how many have run. Never edit a released entry: append one.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE redirects (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        slug       TEXT    NOT NULL UNIQUE,
        name       TEXT    NOT NULL,
        target     TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        updated_at TEXT    NOT NULL
    )
    """,
    # Server-side values that must survive a restart: the session secret, the API key's hash
    """
    CREATE TABLE settings (
        name  TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

BUSY_TIMEOUT_SECONDS = 5.0
# Ids are SQLite rowids: positive 64-bit integers. Anything else names no redirect (and would overflow the driver).
MAX_ID = 2**63 - 1
_COLUMNS = "id, slug, name, target, created_at, updated_at"


class DatabaseError(RuntimeError):
    """The database cannot be opened, migrated or reached."""


class SlugTakenError(Exception):
    """Another redirect already uses this slug."""


@dataclass(frozen=True)
class Redirect:
    id: int
    slug: str
    name: str
    target: str
    created_at: str
    updated_at: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(row: sqlite3.Row) -> Redirect:
    return Redirect(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        target=row["target"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _is_possible_id(redirect_id: int) -> bool:
    return 1 <= redirect_id <= MAX_ID


def _is_slug_conflict(exc: sqlite3.IntegrityError) -> bool:
    return "redirects.slug" in str(exc)


class Database:
    def __init__(self, path: Path, clock: Clock = utc_now) -> None:
        self.path = path
        self.clock = clock

    @contextmanager
    def _connect(self, create: bool = False) -> Iterator[sqlite3.Connection]:
        """A connection inside one transaction: committed on success, rolled back on error, always closed.

        Only migrate() may create the file: at run time a missing database is an error, never a new empty one.
        """
        uri = f"{self.path.resolve().as_uri()}?mode={'rwc' if create else 'rw'}"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        with closing(conn):
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def migrate(self) -> None:
        """Create the data directory and the database if needed, then apply pending migrations."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect(create=True) as conn:
                # Explicit transaction: DDL would otherwise autocommit, and a second process waits here
                conn.execute("BEGIN IMMEDIATE")
                version: int = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise DatabaseError(
                        f"{self.path} has schema version {version}, newer than this release"
                        f" ({len(MIGRATIONS)}). Run a newer Redirect or restore a backup."
                    )
                for number, statement in enumerate(MIGRATIONS[version:], start=version + 1):
                    conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {number}")
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(
                f"Cannot open the database {self.path}: {exc}."
                f" Check that DATA_DIR ({self.path.parent}) exists and is writable by this user."
            ) from exc

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM redirects LIMIT 1").fetchall()

    def list_all(self) -> list[Redirect]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM redirects ORDER BY name COLLATE NOCASE, slug").fetchall()
        return [_row(row) for row in rows]

    def get(self, redirect_id: int) -> Redirect | None:
        if not _is_possible_id(redirect_id):
            return None
        with self._connect() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM redirects WHERE id = ?", (redirect_id,)).fetchone()
        return _row(row) if row else None

    def target_for(self, slug: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT target FROM redirects WHERE slug = ?", (slug,)).fetchone()
        return str(row["target"]) if row else None

    def create(self, slug: str, name: str, target: str) -> Redirect:
        now = timestamp(self.clock())
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO redirects (slug, name, target, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (slug, name, target, now, now),
                )
        except sqlite3.IntegrityError as exc:
            if _is_slug_conflict(exc):
                raise SlugTakenError(slug) from exc
            raise
        assert cursor.lastrowid is not None
        return Redirect(cursor.lastrowid, slug, name, target, now, now)

    def update(self, redirect_id: int, slug: str, name: str, target: str) -> Redirect | None:
        if not _is_possible_id(redirect_id):
            return None
        now = timestamp(self.clock())
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "UPDATE redirects SET slug = ?, name = ?, target = ?, updated_at = ? WHERE id = ?"
                    f" RETURNING {_COLUMNS}",
                    (slug, name, target, now, redirect_id),
                ).fetchall()
        except sqlite3.IntegrityError as exc:
            if _is_slug_conflict(exc):
                raise SlugTakenError(slug) from exc
            raise
        return _row(rows[0]) if rows else None

    def delete(self, redirect_id: int) -> Redirect | None:
        if not _is_possible_id(redirect_id):
            return None
        with self._connect() as conn:
            # RETURNING rows are read in full before the commit
            rows = conn.execute(f"DELETE FROM redirects WHERE id = ? RETURNING {_COLUMNS}", (redirect_id,)).fetchall()
        return _row(rows[0]) if rows else None

    def setting(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return str(row["value"]) if row else None

    def setting_or_create(self, name: str, make: Callable[[], str]) -> str:
        """The stored value, or `make()` stored now. Two processes starting together keep the same value."""
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO settings (name, value) VALUES (?, ?)", (name, make()))
            row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return str(row["value"])

    def set_settings(self, values: dict[str, str]) -> None:
        """Stores several values in one transaction: they change together or not at all."""
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                values.items(),
            )

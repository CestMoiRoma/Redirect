"""Shared fixtures: the real app on a temporary SQLite database, with a clock the test controls."""

import itertools
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import LOCAL_CLIENT, PUBLIC_BASE_URL, FakeAnnouncer, FrozenClock

import redirect.db
from redirect.config import Settings
from redirect.main import create_app


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def make_client(tmp_path: Path, clock: FrozenClock) -> Iterator[Callable[..., TestClient]]:
    """Factory: a started TestClient (lifespan run, database migrated) on tmp_path, closed after the test."""
    clients: list[TestClient] = []
    ids = (f"id{number:06d}" for number in itertools.count(1))

    def make(
        public_base_url: str | None = None,
        admin_password: str | None = None,
        new_key: Callable[[], str] | None = None,
        api_enabled: bool | None = None,
        local_only: bool | None = None,
        port: int = 80,
        **client_options: object,
    ) -> TestClient:
        settings = Settings(
            data_dir=tmp_path / "data",
            port=port,
            public_base_url=public_base_url,
            admin_password=admin_password,
            api_enabled=api_enabled,
            local_only=local_only,
        )
        announcer = FakeAnnouncer()
        # Predictable ids, in creation order, shared by every app of the test (they use the same database)
        extra = {} if new_key is None else {"new_key": new_key}
        app = create_app(settings, clock, announcer=announcer, make_code=lambda: next(ids), **extra)
        app.state.announcer = announcer
        client = TestClient(app, **{"client": LOCAL_CLIENT, **client_options})
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    """No PUBLIC_BASE_URL: links use the default hostname, redirect.lan."""
    return make_client()


@pytest.fixture
def public_client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(public_base_url=PUBLIC_BASE_URL)


@pytest.fixture
def locked_database(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Another process holds an exclusive lock on the database; the app gives up after 0.1 s instead of 5 s."""
    monkeypatch.setattr(redirect.db, "BUSY_TIMEOUT_SECONDS", 0.1)
    holder = sqlite3.connect(tmp_path / "data" / "redirect.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    yield
    holder.rollback()
    holder.close()

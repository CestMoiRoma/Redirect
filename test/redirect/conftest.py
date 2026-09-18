"""Shared fixtures: the real app on a temporary SQLite database, with a clock the test controls."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import PUBLIC_BASE_URL, FrozenClock

from redirect.config import Settings
from redirect.main import create_app


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def make_client(tmp_path: Path, clock: FrozenClock) -> Iterator[Callable[..., TestClient]]:
    """Factory: a started TestClient (lifespan run, database migrated) on tmp_path, closed after the test."""
    clients: list[TestClient] = []

    def make(public_base_url: str | None = None, **client_options: object) -> TestClient:
        settings = Settings(data_dir=tmp_path / "data", public_base_url=public_base_url)
        client = TestClient(create_app(settings, clock), **client_options)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    """No PUBLIC_BASE_URL: links are built from the Host header (http://testserver)."""
    return make_client()


@pytest.fixture
def public_client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(public_base_url=PUBLIC_BASE_URL)

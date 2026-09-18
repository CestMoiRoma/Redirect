"""Helpers shared by the test modules (imported directly; fixtures live in conftest.py)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

START = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)
PUBLIC_BASE_URL = "http://redirect.local:8080"


class FrozenClock:
    """Backend clock: stands still until the test moves it."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def create(client: TestClient, slug: str, name: str = "Some name", target: str = "http://example.test/") -> dict:
    """POST a valid redirect and return its JSON body."""
    response = client.post("/api/redirects", json={"slug": slug, "name": name, "target": target})
    assert response.status_code == 201, response.text
    return response.json()

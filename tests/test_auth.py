"""W3: auth + role enforcement.

These tests use FastAPI dependency overrides to inject a fake current user.
The real OIDC handshake (authlib + provider) is verified manually via `make dev`
once oidc-mock is wired up.
"""

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import UserRole
from app.db.models import AppUser


def _make_user(role: UserRole) -> AppUser:
    return AppUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email=f"{role.value}@acme.test",
        display_name=role.value.title(),
        role=role,
        is_active=True,
    )


@pytest.fixture
def signed_in() -> Iterator[Callable[[UserRole], TestClient]]:
    """Return a factory: signed_in(role) -> TestClient with that role's current user."""

    def _make(role: UserRole) -> TestClient:
        user = _make_user(role)
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_current_user, None)


def test_me_anonymous_returns_401(client: TestClient) -> None:
    response = client.get("/api/me")
    assert response.status_code == 401


def test_me_authenticated_returns_profile(
    signed_in: Callable[[UserRole], TestClient],
) -> None:
    client = signed_in(UserRole.marketer)
    response = client.get("/api/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "marketer@acme.test"
    assert body["role"] == "marketer"


def test_viewer_blocked_from_marketer_endpoint(
    signed_in: Callable[[UserRole], TestClient],
) -> None:
    client = signed_in(UserRole.viewer)
    response = client.get("/api/_protected/marketer")
    assert response.status_code == 403


def test_marketer_passes_marketer_endpoint(
    signed_in: Callable[[UserRole], TestClient],
) -> None:
    client = signed_in(UserRole.marketer)
    response = client.get("/api/_protected/marketer")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_manager_passes_marketer_endpoint(
    signed_in: Callable[[UserRole], TestClient],
) -> None:
    """Higher roles satisfy lower-role requirements."""
    client = signed_in(UserRole.manager)
    response = client.get("/api/_protected/marketer")
    assert response.status_code == 200

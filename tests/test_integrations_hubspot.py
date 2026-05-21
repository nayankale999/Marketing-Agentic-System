"""W11 — HubSpot connector + /api/integrations/hubspot endpoints (E12-S01)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import UserRole
from app.db.models import AppUser, IntegrationCredential, Tenant
from app.integrations.credentials import EncryptedPayload, get_encrypted_payload
from app.integrations.hubspot import HubSpotConnector

# -- Fernet encryption helper ------------------------------------------------


def test_encrypted_payload_round_trips() -> None:
    enc = EncryptedPayload(Fernet.generate_key().decode())
    ciphertext = enc.encrypt({"access_token": "abc", "refresh_token": "rt"})
    assert isinstance(ciphertext, bytes)
    assert b"access_token" not in ciphertext  # actually encrypted
    assert enc.decrypt(ciphertext) == {"access_token": "abc", "refresh_token": "rt"}


def test_decrypt_with_wrong_key_raises() -> None:
    from app.integrations.credentials import CredentialDecryptionError

    enc_a = EncryptedPayload(Fernet.generate_key().decode())
    enc_b = EncryptedPayload(Fernet.generate_key().decode())
    ciphertext = enc_a.encrypt({"k": "v"})
    with pytest.raises(CredentialDecryptionError):
        enc_b.decrypt(ciphertext)


# -- HubSpotConnector (respx-mocked) -----------------------------------------


def test_authorize_url_carries_state_scopes_and_redirect_uri() -> None:
    connector = HubSpotConnector(client_id="cid", client_secret="csecret")
    url = connector.authorize_url(
        state="xyz", redirect_uri="http://app/cb", scopes=["contacts.read"]
    )
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "app.hubspot.com"
    assert qs["client_id"] == ["cid"]
    assert qs["state"] == ["xyz"]
    assert qs["redirect_uri"] == ["http://app/cb"]
    assert qs["scope"] == ["contacts.read"]


@respx.mock
async def test_exchange_code_parses_token_response() -> None:
    respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
            },
        )
    )
    connector = HubSpotConnector(client_id="cid", client_secret="csecret")
    before = datetime.now(UTC)
    tokens = await connector.exchange_code(code="abc", redirect_uri="http://app/cb")
    assert tokens.access_token == "at-1"
    assert tokens.refresh_token == "rt-1"
    assert before + timedelta(seconds=3590) <= tokens.expires_at <= before + timedelta(seconds=3610)


@respx.mock
async def test_refresh_returns_new_access_token() -> None:
    respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 1800},
        )
    )
    connector = HubSpotConnector(client_id="cid", client_secret="csecret")
    tokens = await connector.refresh(refresh_token="rt-1")
    assert tokens.access_token == "at-2"


@respx.mock
async def test_list_contacts_maps_records_and_cursor() -> None:
    respx.get("https://api.hubapi.com/crm/v3/objects/contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "1",
                        "properties": {
                            "email": "a@x.test",
                            "firstname": "A",
                            "hs_lastmodifieddate": "2026-05-01T10:00:00.000Z",
                        },
                    },
                    {"id": "2", "properties": {"email": "b@x.test"}},
                ],
                "paging": {"next": {"after": "cursor-abc"}},
            },
        )
    )
    connector = HubSpotConnector(client_id="cid", client_secret="csecret")
    records, next_after = await connector.list_contacts(access_token="at-1", limit=10)
    assert next_after == "cursor-abc"
    assert [r.external_id for r in records] == ["1", "2"]
    assert records[0].properties["email"] == "a@x.test"
    assert records[0].updated_at is not None


# -- API endpoints -----------------------------------------------------------


@pytest.fixture
async def tenant_in_db(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"hs-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _persist_user(engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@hs.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def admin_client(
    override_api_db,
    db_engine: AsyncEngine,
    tenant_in_db: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator:
    """Yield (httpx client signed in as admin, the user) with HUBSPOT_* set."""
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "HUBSPOT_REDIRECT_URI", "http://localhost:8001/api/integrations/hubspot/callback"
    )
    # Settings are LRU-cached on first access; clear so the env overrides take.
    from app.settings.config import get_settings

    get_settings.cache_clear()

    user = await _persist_user(db_engine, tenant_in_db, UserRole.admin)
    app.dependency_overrides[get_current_user] = lambda: user

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client, user
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_current_user, None)
        get_settings.cache_clear()


async def test_connect_endpoint_redirects_to_hubspot_authorize(admin_client) -> None:
    client, _ = admin_client
    resp = await client.get("/api/integrations/hubspot/connect")
    assert resp.status_code == 302
    assert "app.hubspot.com/oauth/authorize" in resp.headers["location"]
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["client_id"] == ["test-client-id"]
    assert "state" in qs and len(qs["state"][0]) >= 16


async def test_connect_requires_admin(
    override_api_db,
    db_engine: AsyncEngine,
    tenant_in_db: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUBSPOT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("HUBSPOT_CLIENT_SECRET", "test-client-secret")
    from app.settings.config import get_settings

    get_settings.cache_clear()

    viewer = await _persist_user(db_engine, tenant_in_db, UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: viewer
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/integrations/hubspot/connect")
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        get_settings.cache_clear()


async def test_callback_rejects_bad_state(admin_client) -> None:
    client, _ = admin_client
    # Hit /connect first to set the state cookie -- but then send a different one.
    await client.get("/api/integrations/hubspot/connect")
    resp = await client.get("/api/integrations/hubspot/callback?code=fake&state=does-not-match")
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"]


@respx.mock
async def test_callback_stores_encrypted_credential(
    admin_client, db_engine: AsyncEngine, tenant_in_db: uuid.UUID
) -> None:
    client, _ = admin_client
    respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at-stored",
                "refresh_token": "rt-stored",
                "expires_in": 3600,
            },
        )
    )

    # Step 1: /connect to get state cookie
    redirect = await client.get("/api/integrations/hubspot/connect")
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]

    # Step 2: simulate HubSpot redirecting to /callback?code=...&state=...
    resp = await client.get(f"/api/integrations/hubspot/callback?code=auth-code-xyz&state={state}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "connected", "provider": "hubspot"}

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        cred = (
            await session.execute(
                select(IntegrationCredential).where(
                    IntegrationCredential.tenant_id == tenant_in_db,
                    IntegrationCredential.provider == "hubspot",
                )
            )
        ).scalar_one()
        assert cred.expires_at is not None
        assert b"at-stored" not in bytes(cred.encrypted_payload)  # actually encrypted
        decrypted = get_encrypted_payload().decrypt(cred.encrypted_payload)
        assert decrypted["access_token"] == "at-stored"
        assert decrypted["refresh_token"] == "rt-stored"


@respx.mock
async def test_test_endpoint_returns_contacts(
    admin_client, db_engine: AsyncEngine, tenant_in_db: uuid.UUID
) -> None:
    """`/api/integrations/hubspot/test` lists contacts via the stored credential."""
    client, _ = admin_client
    # Seed a credential directly.
    enc = get_encrypted_payload()
    payload = enc.encrypt({"access_token": "stored-at", "refresh_token": "rt", "scopes": []})
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            IntegrationCredential(
                tenant_id=tenant_in_db,
                provider="hubspot",
                label="default",
                encrypted_payload=payload,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    contacts_route = respx.get("https://api.hubapi.com/crm/v3/objects/contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "10", "properties": {"email": "x@y.test"}},
                    {"id": "11", "properties": {"email": "z@y.test"}},
                ],
                "paging": {"next": {"after": "next-cursor"}},
            },
        )
    )

    resp = await client.post("/api/integrations/hubspot/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert body["next_after"] == "next-cursor"
    assert contacts_route.called
    # The request used the stored access_token.
    sent_request = contacts_route.calls.last.request
    assert sent_request.headers["authorization"] == "Bearer stored-at"


@respx.mock
async def test_test_endpoint_refreshes_when_near_expiry(
    admin_client, db_engine: AsyncEngine, tenant_in_db: uuid.UUID
) -> None:
    """If `expires_at` is in the past, refresh first then list."""
    client, _ = admin_client
    enc = get_encrypted_payload()
    payload = enc.encrypt({"access_token": "stale-at", "refresh_token": "the-rt", "scopes": []})
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            IntegrationCredential(
                tenant_id=tenant_in_db,
                provider="hubspot",
                label="default",
                encrypted_payload=payload,
                expires_at=datetime.now(UTC) - timedelta(seconds=5),  # expired
            )
        )

    refresh_route = respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fresh-at", "refresh_token": "the-rt", "expires_in": 3600},
        )
    )
    contacts_route = respx.get("https://api.hubapi.com/crm/v3/objects/contacts").mock(
        return_value=httpx.Response(200, json={"results": [], "paging": {}})
    )

    resp = await client.post("/api/integrations/hubspot/test")
    assert resp.status_code == 200
    assert refresh_route.called, "refresh should fire when credential is past expiry"
    # Contacts request uses the fresh token, not the stale one.
    used = contacts_route.calls.last.request.headers["authorization"]
    assert used == "Bearer fresh-at"


async def test_test_endpoint_404_when_no_credential(admin_client) -> None:
    client, _ = admin_client
    resp = await client.post("/api/integrations/hubspot/test")
    assert resp.status_code == 404

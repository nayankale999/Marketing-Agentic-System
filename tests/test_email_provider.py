"""W27 — Email provider integration (E12-S02).

Three layers:
  * Config CRUD — encrypt, redact api_key on read, default_from_email must
    be in verified_senders.
  * Send-test — round-trips through the SendGrid connector with respx
    intercepting the API call.
  * Webhook ingestion — bearer-secret auth, normalised event mapping,
    idempotency via provider_event_id, suppression rows for terminal events.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import ChannelPlatform, EventKind, UserRole
from app.db.models import (
    AnalyticEvent,
    AppUser,
    IntegrationCredential,
    SuppressionEntry,
    Tenant,
)
from app.integrations.credentials import get_encrypted_payload


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"em-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@em.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def tenant_id(override_api_db, db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_tenant(db_engine)


@pytest.fixture
async def admin_client(
    tenant_id, db_engine: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    user = await _make_user(db_engine, tenant_id, UserRole.admin)
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def webhook_client(
    override_api_db,
) -> AsyncIterator[httpx.AsyncClient]:
    """No auth override — exercises the public webhook endpoint."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _valid_config_body() -> dict:
    return {
        "provider": "sendgrid",
        "api_key": "sg.test-key-abcd1234",
        "default_from_email": "alex@acme.com",
        "verified_senders": ["alex@acme.com", "noreply@acme.com"],
        "webhook_secret": "wh-secret-xyz",
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


async def test_post_config_encrypts_payload_and_redacts_api_key(
    admin_client, db_engine: AsyncEngine, tenant_id
) -> None:
    resp = await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["provider"] == "sendgrid"
    assert body["api_key_last4"] == "1234"
    assert body["default_from_email"] == "alex@acme.com"
    assert set(body["verified_senders"]) == {"alex@acme.com", "noreply@acme.com"}
    assert body["has_webhook_secret"] is True

    # The stored payload is encrypted bytes; decrypt round-trips to the input.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(
                select(IntegrationCredential).where(
                    IntegrationCredential.tenant_id == tenant_id,
                    IntegrationCredential.provider == "sendgrid",
                )
            )
        ).scalar_one()
        decrypted = get_encrypted_payload().decrypt(row.encrypted_payload)
        assert decrypted["api_key"] == "sg.test-key-abcd1234"
        assert decrypted["webhook_secret"] == "wh-secret-xyz"


async def test_post_config_rejects_default_not_in_verified(
    admin_client,
) -> None:
    body = _valid_config_body()
    body["default_from_email"] = "marketing@acme.com"  # not in verified_senders
    resp = await admin_client.post("/api/integrations/email/config", json=body)
    assert resp.status_code == 422


async def test_post_config_rejects_unknown_provider(admin_client) -> None:
    body = _valid_config_body()
    body["provider"] = "mailwhomever"
    resp = await admin_client.post("/api/integrations/email/config", json=body)
    assert resp.status_code == 422


async def test_post_config_upserts_existing_row(admin_client) -> None:
    r1 = await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    body = _valid_config_body()
    body["api_key"] = "sg.different-key-9999"
    r2 = await admin_client.post("/api/integrations/email/config", json=body)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["api_key_last4"] == "9999"


async def test_get_config_404_when_unconfigured(admin_client) -> None:
    resp = await admin_client.get("/api/integrations/email/config")
    assert resp.status_code == 404


async def test_get_config_returns_redacted_payload(admin_client) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    resp = await admin_client.get("/api/integrations/email/config")
    assert resp.status_code == 200
    assert resp.json()["api_key_last4"] == "1234"
    assert "api_key" not in resp.json()  # never returned in full


@pytest.mark.parametrize("role", [UserRole.manager, UserRole.marketer, UserRole.viewer])
async def test_non_admin_cannot_manage_config(
    db_engine: AsyncEngine, tenant_id, role, override_api_db
) -> None:
    user = await _make_user(db_engine, tenant_id, role)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    "/api/integrations/email/config", json=_valid_config_body()
                )
            ).status_code == 403
            assert (await client.get("/api/integrations/email/config")).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Send-test
# ---------------------------------------------------------------------------


@respx.mock
async def test_send_test_round_trip(admin_client) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-abc"})
    )
    resp = await admin_client.post(
        "/api/integrations/email/send-test",
        json={
            "to_email": "qa@acme.com",
            "from_email": "alex@acme.com",
            "subject": "ping",
            "body": "test",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["provider"] == "sendgrid"
    assert body["provider_message_id"] == "msg-abc.0"


@respx.mock
async def test_send_test_uses_default_from_when_omitted(admin_client) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    route = respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-2"})
    )
    resp = await admin_client.post(
        "/api/integrations/email/send-test",
        json={"to_email": "qa@acme.com"},
    )
    assert resp.status_code == 200
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["from"]["email"] == "alex@acme.com"


async def test_send_test_rejects_unverified_sender(admin_client) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    resp = await admin_client.post(
        "/api/integrations/email/send-test",
        json={"to_email": "qa@acme.com", "from_email": "rogue@elsewhere.com"},
    )
    assert resp.status_code == 422


async def test_send_test_404_when_unconfigured(admin_client) -> None:
    resp = await admin_client.post(
        "/api/integrations/email/send-test",
        json={"to_email": "qa@acme.com"},
    )
    assert resp.status_code == 404


@respx.mock
async def test_send_test_returns_error_on_provider_unreachable(
    admin_client,
) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    respx.post(_SENDGRID_API).mock(return_value=httpx.Response(503, text="boom"))
    resp = await admin_client.post(
        "/api/integrations/email/send-test",
        json={"to_email": "qa@acme.com"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert "provider_unreachable" in body["error"]


# ---------------------------------------------------------------------------
# Webhook ingest
# ---------------------------------------------------------------------------


def _sendgrid_event(
    *, event: str, event_id: str, email: str = "u@acme.com", ts: int = 1717160000
) -> dict:
    return {
        "event": event,
        "sg_event_id": event_id,
        "sg_message_id": f"msg-{event_id}",
        "email": email,
        "timestamp": ts,
    }


async def test_webhook_requires_shared_secret(
    admin_client, webhook_client, tenant_id
) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    resp = await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}",
        content=json.dumps([_sendgrid_event(event="open", event_id="e1")]),
        headers={"Content-Type": "application/json"},  # missing secret header
    )
    assert resp.status_code == 401


async def test_webhook_404_for_unknown_tenant(webhook_client) -> None:
    resp = await webhook_client.post(
        f"/api/integrations/email/webhook/{uuid.uuid4()}",
        content="[]",
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "anything",
        },
    )
    assert resp.status_code == 404


async def test_webhook_ingests_events_and_writes_suppressions(
    admin_client, webhook_client, db_engine: AsyncEngine, tenant_id
) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    payload = [
        _sendgrid_event(event="open", event_id="e1", email="a@acme.com"),
        _sendgrid_event(event="click", event_id="e2", email="a@acme.com"),
        _sendgrid_event(event="bounce", event_id="e3", email="bouncer@acme.com"),
        _sendgrid_event(event="spamreport", event_id="e4", email="spam@acme.com"),
        _sendgrid_event(event="unsubscribe", event_id="e5", email="unsub@acme.com"),
        _sendgrid_event(event="deferred", event_id="e6"),  # 'other' → not written
    ]
    resp = await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "wh-secret-xyz",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received"] == 6
    assert body["written"] == 5  # deferred ('other') skipped
    assert body["suppression_writes"] == 3

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        events = (
            await session.execute(
                select(AnalyticEvent).where(AnalyticEvent.tenant_id == tenant_id)
            )
        ).scalars().all()
        kinds = {e.event_type for e in events}
        assert kinds == {
            EventKind.open,
            EventKind.click,
            EventKind.bounce,
            EventKind.spam_complaint,
            EventKind.unsubscribe,
        }

        suppressions = (
            await session.execute(
                select(SuppressionEntry).where(SuppressionEntry.tenant_id == tenant_id)
            )
        ).scalars().all()
        reasons = {(s.identifier, s.reason) for s in suppressions}
        assert reasons == {
            ("bouncer@acme.com", "bounce"),
            ("spam@acme.com", "complaint"),
            ("unsub@acme.com", "unsubscribe"),
        }


async def test_webhook_dedups_repeat_event_ids(
    admin_client, webhook_client, db_engine: AsyncEngine, tenant_id
) -> None:
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    payload = json.dumps([_sendgrid_event(event="open", event_id="same-id")])
    headers = {
        "Content-Type": "application/json",
        "X-MAS-Webhook-Secret": "wh-secret-xyz",
    }
    r1 = await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}",
        content=payload, headers=headers,
    )
    r2 = await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}",
        content=payload, headers=headers,
    )
    assert r1.json()["written"] == 1
    assert r2.json()["deduped"] == 1
    assert r2.json()["written"] == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(AnalyticEvent).where(
                    AnalyticEvent.tenant_id == tenant_id,
                    AnalyticEvent.provider_event_id == "same-id",
                )
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_webhook_idempotent_suppression(
    admin_client, webhook_client, db_engine: AsyncEngine, tenant_id
) -> None:
    """Replaying a bounce event doesn't create a duplicate suppression row."""
    await admin_client.post(
        "/api/integrations/email/config", json=_valid_config_body()
    )
    headers = {
        "Content-Type": "application/json",
        "X-MAS-Webhook-Secret": "wh-secret-xyz",
    }
    payload_a = json.dumps([_sendgrid_event(event="bounce", event_id="b1", email="dup@acme.com")])
    payload_b = json.dumps([_sendgrid_event(event="bounce", event_id="b2", email="dup@acme.com")])
    await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}", content=payload_a, headers=headers
    )
    r2 = await webhook_client.post(
        f"/api/integrations/email/webhook/{tenant_id}", content=payload_b, headers=headers
    )
    # Second event writes a new analytic_event row but the suppression entry
    # already exists for dup@acme.com → not re-written.
    assert r2.json()["written"] == 1
    assert r2.json()["suppression_writes"] == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        sups = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.tenant_id == tenant_id,
                    SuppressionEntry.identifier == "dup@acme.com",
                )
            )
        ).scalars().all()
        assert len(sups) == 1

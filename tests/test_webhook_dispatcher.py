"""W33 — Webhook dispatcher (E12-S06).

Coverage:
  * Unknown provider → 404 (no raw row written).
  * Valid signature → events written, raw row mapped, summary returned.
  * Replay of same event id → dedup, second call writes 0 events.
  * Invalid/missing signature → 401, raw row persisted with reason,
    audit row written.
  * LinkedIn stub → 200, raw row marked unmapped (always-empty mapper).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import EventKind, UserRole
from app.db.models import (
    AnalyticEvent,
    AppUser,
    AuditLog,
    IntegrationCredential,
    RawWebhook,
    Tenant,
)
from app.integrations.credentials import get_encrypted_payload


async def _seed_tenant_with_email_cred(
    db_engine: AsyncEngine,
    *,
    secret: str | None = "wh-secret",
    provider: str = "sendgrid",
) -> uuid.UUID:
    payload = {
        "api_key": "sg.test",
        "default_from_email": "alex@acme.com",
        "verified_senders": ["alex@acme.com"],
        "webhook_secret": secret,
    }
    encrypted = get_encrypted_payload().encrypt(payload)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"wh-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        session.add(
            IntegrationCredential(
                tenant_id=tenant.id,
                channel_id=None,
                provider=provider,
                label="default",
                encrypted_payload=encrypted,
                key_version=1,
            )
        )
        return tenant.id


def _sendgrid_event(*, event: str, event_id: str, email: str = "u@cust.com", ts: int = 1717000000) -> dict:
    return {
        "event": event,
        "sg_event_id": event_id,
        "sg_message_id": f"msg-{event_id}",
        "email": email,
        "timestamp": ts,
    }


@pytest.fixture
async def webhook_client(override_api_db) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Unknown provider → 404
# ---------------------------------------------------------------------------


async def test_unknown_provider_returns_404(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine)
    resp = await webhook_client.post(
        f"/webhooks/madeup-provider/{tenant_id}",
        content="{}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404

    # No raw row should have been written.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(RawWebhook).where(RawWebhook.provider == "madeup-provider")
            )
        ).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# Valid signature → events written + raw row mapped
# ---------------------------------------------------------------------------


async def test_valid_sendgrid_webhook_maps_events_and_links_raw_row(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine)
    payload = json.dumps(
        [
            _sendgrid_event(event="open", event_id="e1"),
            _sendgrid_event(event="click", event_id="e2"),
            _sendgrid_event(event="bounce", event_id="e3", email="bouncer@cust.com"),
        ]
    )
    resp = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "wh-secret",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["events_written"] == 3
    assert body["events_deduped"] == 0
    assert body["signature_valid"] is True

    raw_id = uuid.UUID(body["raw_webhook_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        raw = await session.get(RawWebhook, raw_id)
        assert raw is not None
        assert raw.tenant_id == tenant_id
        assert raw.provider == "sendgrid"
        assert raw.signature_valid is True
        assert raw.mapped_event_id is not None  # linked to first event

        # Secret header was redacted in the persisted snapshot.
        assert raw.headers.get("x-mas-webhook-secret") == "***redacted***" or raw.headers.get(
            "X-MAS-Webhook-Secret"
        ) == "***redacted***"

        events = (
            await session.execute(
                select(AnalyticEvent).where(AnalyticEvent.tenant_id == tenant_id)
            )
        ).scalars().all()
        kinds = {e.event_type for e in events}
        assert kinds == {EventKind.open, EventKind.click, EventKind.bounce}


async def test_replay_dedups_via_provider_event_id(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine)
    payload = json.dumps([_sendgrid_event(event="open", event_id="same-id")])
    headers = {"Content-Type": "application/json", "X-MAS-Webhook-Secret": "wh-secret"}

    r1 = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}", content=payload, headers=headers
    )
    r2 = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}", content=payload, headers=headers
    )
    assert r1.json()["events_written"] == 1
    assert r2.json()["events_deduped"] == 1
    assert r2.json()["events_written"] == 0


# ---------------------------------------------------------------------------
# Invalid signature → 401 + raw stored + audit row
# ---------------------------------------------------------------------------


async def test_invalid_signature_returns_401_and_persists_raw_row(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine)
    payload = json.dumps([_sendgrid_event(event="open", event_id="x")])
    resp = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "wrong-secret",
        },
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["reason"] == "secret_mismatch"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        raw = await session.get(RawWebhook, uuid.UUID(detail["raw_webhook_id"]))
        assert raw is not None
        assert raw.signature_valid is False
        assert raw.signature_reason == "secret_mismatch"
        # No analytic_event rows were written.
        events = (
            await session.execute(
                select(AnalyticEvent).where(AnalyticEvent.tenant_id == tenant_id)
            )
        ).scalars().all()
        assert events == []

        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "raw_webhook",
                    AuditLog.entity_id == raw.id,
                    AuditLog.action == "webhook_signature_failed",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert audits[0].extra_metadata["provider"] == "sendgrid"
        assert audits[0].extra_metadata["reason"] == "secret_mismatch"


async def test_missing_signature_header_returns_401(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine)
    resp = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}",
        content="[]",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "missing_header"


async def test_no_credential_returns_401(
    webhook_client, db_engine: AsyncEngine
) -> None:
    """No IntegrationCredential row at all for this tenant + provider →
    handler can't verify → 401 with no_credential reason."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"nocred-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    resp = await webhook_client.post(
        f"/webhooks/sendgrid/{tenant_id}",
        content="[]",
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "anything",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "no_credential"


# ---------------------------------------------------------------------------
# LinkedIn stub — always unmapped
# ---------------------------------------------------------------------------


async def test_linkedin_stub_handler_lands_as_unmapped(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(
        db_engine, provider="linkedin"
    )
    body = json.dumps({"events": [{"id": "li-1", "type": "post.created"}]})
    resp = await webhook_client.post(
        f"/webhooks/linkedin/{tenant_id}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "wh-secret",
        },
    )
    assert resp.status_code == 200
    body_out = resp.json()
    assert body_out["signature_valid"] is True
    assert body_out["events_written"] == 0  # stub mapper returns []

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        raw = await session.get(RawWebhook, uuid.UUID(body_out["raw_webhook_id"]))
        assert raw is not None
        assert raw.provider == "linkedin"
        assert raw.signature_valid is True
        assert raw.mapped_event_id is None  # unmapped


# ---------------------------------------------------------------------------
# Body persistence — even when handler maps zero events, the raw payload
# is retrievable for debugging.
# ---------------------------------------------------------------------------


async def test_raw_payload_bytes_persisted(
    webhook_client, db_engine: AsyncEngine
) -> None:
    tenant_id = await _seed_tenant_with_email_cred(db_engine, provider="linkedin")
    body_bytes = b'{"id": "preserve-me"}'
    resp = await webhook_client.post(
        f"/webhooks/linkedin/{tenant_id}",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-MAS-Webhook-Secret": "wh-secret",
        },
    )
    raw_id = uuid.UUID(resp.json()["raw_webhook_id"])
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        raw = await session.get(RawWebhook, raw_id)
        assert bytes(raw.payload) == body_bytes

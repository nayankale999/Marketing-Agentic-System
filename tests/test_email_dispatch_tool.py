"""W27 — EmailDispatchTool tests (E11-S06).

The cross-provider concerns the tool owns:
  * suppression filter
  * sender + recipient + header validation
  * normalised result shape
  * provider-tagged errors bubble up
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import ChannelPlatform
from app.db.models import SuppressionEntry, Tenant
from app.db.session import set_tenant_context
from app.integrations.email import SendGridConnector
from app.integrations.email.base import (
    EmailConnector,
    EmailMessage,
    EmailRecipient,
    ProviderUnreachableError,
    SendResult,
)
from app.tools.email_dispatch import DispatchValidationError, EmailDispatchTool


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


def _connector(**payload_overrides) -> SendGridConnector:
    payload = {
        "api_key": "sg.test-key-1234",
        "default_from_email": "alex@acme.com",
        "verified_senders": ["alex@acme.com", "marketing@acme.com"],
        "webhook_secret": "wh",
    }
    payload.update(payload_overrides)
    return SendGridConnector(payload=payload)


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"dispatch-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _seed_suppression(
    db_engine: AsyncEngine, tenant_id: uuid.UUID, emails: list[str]
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        for email in emails:
            session.add(
                SuppressionEntry(
                    tenant_id=tenant_id,
                    channel_platform=ChannelPlatform.email,
                    identifier=email,
                    reason="bounce",
                )
            )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_dispatch_happy_path_returns_normalised_shape(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-base"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [
                    {"email": "first@customer.com", "merge_fields": {"first_name": "First"}},
                    {"email": "second@customer.com"},
                ],
                "message": {
                    "subject": "Welcome",
                    "html_body": "<p>Hi {{first_name}}</p>",
                    "text_body": "Hi {{first_name}}",
                },
                "idempotency_key": "campaign-1:step-1",
            }
        )

    assert result["provider"] == "sendgrid"
    assert result["accepted_count"] == 2
    assert result["rejected_count"] == 0
    assert {r["email"] for r in result["per_message_ids"]} == {
        "first@customer.com",
        "second@customer.com",
    }
    # batch_id is a UUID string the caller can correlate against.
    uuid.UUID(result["batch_id"])


# ---------------------------------------------------------------------------
# Sender validation (E11-S06 #3 + E12-S02 #3)
# ---------------------------------------------------------------------------


async def test_unverified_sender_raises_validation_error(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        with pytest.raises(DispatchValidationError) as exc:
            await tool.call(
                {
                    "from_email": "rogue@elsewhere.com",
                    "audience_batch": [{"email": "x@customer.com"}],
                    "message": {"subject": "x", "text_body": "x"},
                }
            )
        assert "verified_senders" in str(exc.value)


# ---------------------------------------------------------------------------
# Suppression filter (E11-S06 #4)
# ---------------------------------------------------------------------------


@respx.mock
async def test_suppressed_recipients_dropped_and_reported(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _seed_suppression(db_engine, tenant_id, ["bounced@customer.com"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-x"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [
                    {"email": "live@customer.com"},
                    {"email": "bounced@customer.com"},
                ],
                "message": {"subject": "x", "text_body": "x"},
            }
        )

    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 1
    assert result["rejections"][0] == {
        "email": "bounced@customer.com",
        "reason": "suppressed",
    }


@respx.mock
async def test_suppression_check_is_case_insensitive(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    await _seed_suppression(db_engine, tenant_id, ["bouncer@customer.com"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "BOUNCER@customer.com"}],
                "message": {"subject": "x", "text_body": "x"},
            }
        )
    assert result["accepted_count"] == 0
    assert result["rejections"][0]["reason"] == "suppressed"


# ---------------------------------------------------------------------------
# Recipient validation
# ---------------------------------------------------------------------------


@respx.mock
async def test_invalid_recipient_address_dropped(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [
                    {"email": "no-at-sign"},
                    {"email": ""},
                    {"email": "good@customer.com"},
                ],
                "message": {"subject": "x", "text_body": "x"},
            }
        )

    rejections = {r["email"]: r["reason"] for r in result["rejections"]}
    assert rejections.get("no-at-sign") == "invalid_address"
    assert "" in rejections  # missing_email
    assert result["accepted_count"] == 1


@respx.mock
async def test_blocked_domain_dropped(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "qa@example.com"}],
                "message": {"subject": "x", "text_body": "x"},
            }
        )

    assert result["accepted_count"] == 0
    assert result["rejections"][0]["reason"] == "blocked_domain"


# ---------------------------------------------------------------------------
# Forbidden headers (E11-S06 #3)
# ---------------------------------------------------------------------------


async def test_forbidden_header_raises(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        with pytest.raises(DispatchValidationError) as exc:
            await tool.call(
                {
                    "from_email": "alex@acme.com",
                    "audience_batch": [{"email": "ok@customer.com"}],
                    "message": {
                        "subject": "x",
                        "text_body": "x",
                        "headers": {"X-Mailer": "evil"},
                    },
                }
            )
        assert "forbidden headers" in str(exc.value).lower()


async def test_empty_batch_raises(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        with pytest.raises(DispatchValidationError):
            await tool.call(
                {
                    "from_email": "alex@acme.com",
                    "audience_batch": [],
                    "message": {"subject": "x", "text_body": "x"},
                }
            )


# ---------------------------------------------------------------------------
# Provider errors bubble up provider-tagged (E11-S06 #2)
# ---------------------------------------------------------------------------


@respx.mock
async def test_provider_unreachable_bubbles_up(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_SENDGRID_API).mock(return_value=httpx.Response(503, text="boom"))

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        with pytest.raises(ProviderUnreachableError) as exc:
            await tool.call(
                {
                    "from_email": "alex@acme.com",
                    "audience_batch": [{"email": "ok@customer.com"}],
                    "message": {"subject": "x", "text_body": "x"},
                }
            )
        assert exc.value.provider == "sendgrid"


# ---------------------------------------------------------------------------
# SendGrid connector unit tests
# ---------------------------------------------------------------------------


def test_sendgrid_parse_webhook_handles_array_and_dict() -> None:
    connector = _connector()
    payload = [
        {
            "event": "open",
            "sg_event_id": "e1",
            "email": "u@acme.com",
            "timestamp": 1717160000,
        }
    ]
    events = connector.parse_webhook(payload)
    assert len(events) == 1
    assert events[0].event_type == "open"
    assert events[0].email == "u@acme.com"
    assert events[0].provider_event_id == "e1"


def test_sendgrid_parse_webhook_maps_event_types() -> None:
    connector = _connector()
    payload = [
        {"event": "bounce", "sg_event_id": "b1"},
        {"event": "spamreport", "sg_event_id": "s1"},
        {"event": "unsubscribe", "sg_event_id": "u1"},
        {"event": "group_unsubscribe", "sg_event_id": "gu1"},
        {"event": "deferred", "sg_event_id": "d1"},
    ]
    events = connector.parse_webhook(payload)
    types = {e.provider_event_id: e.event_type for e in events}
    assert types["b1"] == "bounce"
    assert types["s1"] == "spam_complaint"
    assert types["u1"] == "unsubscribe"
    assert types["gu1"] == "unsubscribe"
    assert types["d1"] == "other"


def test_sendgrid_parse_webhook_drops_events_without_id() -> None:
    connector = _connector()
    events = connector.parse_webhook([{"event": "open"}])  # no sg_event_id/sg_message_id
    assert events == []


def test_sendgrid_parse_webhook_tolerates_garbage_shape() -> None:
    connector = _connector()
    assert connector.parse_webhook(None) == []
    assert connector.parse_webhook("not a list") == []
    assert connector.parse_webhook(123) == []

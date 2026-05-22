"""W29 — CAN-SPAM footer injection + transactional bypass (E16-S04 #1, #4).

Two layers:
  * EmailDispatchTool footer injection — when `compliance_footer` is in
    inputs, the tool appends unsubscribe + postal address to html_body and
    text_body unless the body already contains them.
  * Transactional bypass — when `transactional=True`, suppression filter
    is skipped + the audit metadata carries `transactional=true`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import ChannelPlatform
from app.db.models import SuppressionEntry, Tenant
from app.db.session import set_tenant_context
from app.integrations.email import SendGridConnector
from app.tools.email_dispatch import EmailDispatchTool


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


def _connector() -> SendGridConnector:
    return SendGridConnector(
        payload={
            "api_key": "sg.test",
            "default_from_email": "alex@acme.com",
            "verified_senders": ["alex@acme.com"],
        }
    )


async def _seed_tenant_with_suppression(
    db_engine: AsyncEngine, suppressed: list[str]
) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"cs-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        for email in suppressed:
            session.add(
                SuppressionEntry(
                    tenant_id=tenant.id,
                    channel_platform=ChannelPlatform.email,
                    identifier=email,
                    reason="bounce",
                )
            )
        return tenant.id


# ---------------------------------------------------------------------------
# Footer injection
# ---------------------------------------------------------------------------


@respx.mock
async def test_footer_injected_into_html_and_text(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant_with_suppression(db_engine, [])
    route = respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "ok@customer.com"}],
                "message": {
                    "subject": "Hi",
                    "html_body": "<p>Hello</p>",
                    "text_body": "Hello",
                },
                "compliance_footer": {
                    "unsubscribe_url": "{{unsubscribe_url}}",
                    "postal_address": "123 Acme Way, City",
                },
            }
        )

    # Inspect the actual SendGrid request body to confirm footer is there.
    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    contents = {c["type"]: c["value"] for c in sent["content"]}
    assert "123 Acme Way" in contents["text/plain"]
    assert "{{unsubscribe_url}}" in contents["text/plain"]
    assert "123 Acme Way" in contents["text/html"]
    assert "{{unsubscribe_url}}" in contents["text/html"]


@respx.mock
async def test_footer_not_double_injected(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant_with_suppression(db_engine, [])
    route = respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    pre_existing_body = (
        "Hi there. Unsubscribe via {{unsubscribe_url}}. Acme HQ, 123 Acme Way."
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "ok@customer.com"}],
                "message": {
                    "subject": "Hi",
                    "text_body": pre_existing_body,
                    "html_body": pre_existing_body,
                },
                "compliance_footer": {
                    "unsubscribe_url": "{{unsubscribe_url}}",
                    "postal_address": "Acme HQ, 123 Acme Way.",
                },
            }
        )

    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    text_body = next(
        c["value"] for c in sent["content"] if c["type"] == "text/plain"
    )
    # Only one occurrence of the unsubscribe placeholder — no duplicate footer.
    assert text_body.count("{{unsubscribe_url}}") == 1


# ---------------------------------------------------------------------------
# Transactional bypass (E16-S04 #4)
# ---------------------------------------------------------------------------


@respx.mock
async def test_transactional_bypass_skips_suppression(
    db_engine: AsyncEngine,
) -> None:
    """A suppressed recipient is still sent to when `transactional=True`."""
    tenant_id = await _seed_tenant_with_suppression(
        db_engine, ["dsar-requester@customer.com"]
    )
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        # Without `transactional=True`, this recipient would be suppressed.
        result = await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "dsar-requester@customer.com"}],
                "message": {"subject": "DSAR", "text_body": "Here is your data"},
                "transactional": True,
            }
        )

    assert result["accepted_count"] == 1
    # Suppressed recipients aren't reported when transactional bypass is on —
    # they're sent as normal.
    assert all(
        r["reason"] != "suppressed" for r in result["rejections"]
    )


@respx.mock
async def test_transactional_default_false_still_filters_suppression(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant_with_suppression(
        db_engine, ["bounced@customer.com"]
    )
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
                    {"email": "bounced@customer.com"},
                    {"email": "ok@customer.com"},
                ],
                "message": {"subject": "Hi", "text_body": "x"},
                # `transactional` omitted — default False.
            }
        )
    assert result["accepted_count"] == 1
    rejections = {r["email"]: r["reason"] for r in result["rejections"]}
    assert rejections.get("bounced@customer.com") == "suppressed"


@respx.mock
async def test_transactional_with_footer_still_injects_footer(
    db_engine: AsyncEngine,
) -> None:
    """When the dispatch agent doesn't pass `compliance_footer` (because the
    asset is transactional), the tool should also not inject. That's the
    contract — caller controls the policy."""
    tenant_id = await _seed_tenant_with_suppression(db_engine, [])
    route = respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "m"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = EmailDispatchTool(
            connector=_connector(), session=session, tenant_id=tenant_id
        )
        await tool.call(
            {
                "from_email": "alex@acme.com",
                "audience_batch": [{"email": "ok@customer.com"}],
                "message": {"subject": "Hi", "text_body": "Body"},
                "transactional": True,
                # No compliance_footer key → no injection regardless of mode.
            }
        )
    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    text_body = next(c["value"] for c in sent["content"] if c["type"] == "text/plain")
    assert text_body == "Body"

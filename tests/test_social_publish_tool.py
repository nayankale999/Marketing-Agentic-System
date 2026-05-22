"""W30 — SocialPublishTool tests (E11-S05).

The tool's job: idempotency on `idempotency_key`, media validation, normalised
result shape. Retry semantics belong to the connector (tested in
test_linkedin_connector.py); the tool surfaces what the connector returns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import AssetStatus, AssetType, CampaignStatus, CampaignType
from app.db.models import (
    Campaign,
    ContentAsset,
    DispatchAttempt,
    Tenant,
)
from app.db.session import set_tenant_context
from app.integrations.social.base import (
    MediaRequiredError,
    OAuthRevokedError,
    ProviderUnreachableError,
)
from app.integrations.social.linkedin import LinkedInConnector
from app.tools.social_publish import (
    SocialPublishError,
    SocialPublishTool,
)


_UGC_URL = LinkedInConnector.UGC_POSTS_URL


def _connector() -> LinkedInConnector:
    return LinkedInConnector(client_id="cid", client_secret="csec")


async def _seed_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"sp-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _seed_asset(
    db_engine: AsyncEngine, tenant_id: uuid.UUID
) -> uuid.UUID:
    """Minimal tenant → campaign → content_asset chain so dispatch_attempt's
    FK to content_asset is satisfiable."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = Campaign(
            tenant_id=tenant_id,
            name=f"sp-camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("100.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        asset = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            asset_type=AssetType.social_post,
            status=AssetStatus.published,
            title="t",
            content="c",
            extra_metadata={"channel_platform": "linkedin"},
            is_required=False,
        )
        session.add(asset)
        await session.flush()
        return asset.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_publish_happy_path_writes_no_attempt_row_but_returns_shape(
    db_engine: AsyncEngine,
) -> None:
    """The tool itself doesn't write `dispatch_attempt` — the dispatch
    handler does, after the call returns. We verify the tool's output
    shape and that it called the platform exactly once."""
    tenant_id = await _seed_tenant(db_engine)
    route = respx.post(_UGC_URL).mock(
        return_value=httpx.Response(201, json={"id": "urn:li:share:777"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        result = await tool.call(
            {
                "platform": "linkedin",
                "page_urn": "urn:li:organization:1",
                "content": {"text": "Hello LinkedIn"},
                "idempotency_key": f"asset:{uuid.uuid4()}:channel:{uuid.uuid4()}",
            }
        )

    assert route.call_count == 1
    assert result["provider_post_id"] == "urn:li:share:777"
    assert result["status"] == "published"
    assert result["idempotent_hit"] is False
    assert "urn:li:share:777" in result["url"]


# ---------------------------------------------------------------------------
# Idempotency (E11-S05 #3)
# ---------------------------------------------------------------------------


@respx.mock
async def test_idempotent_key_returns_cached_without_calling_provider(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    asset_id = await _seed_asset(db_engine, tenant_id)
    idem_key = f"asset:{asset_id}:channel:{uuid.uuid4()}"

    # Pre-seed a `sent` dispatch_attempt for this key.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            DispatchAttempt(
                tenant_id=tenant_id,
                content_asset_id=asset_id,
                recipient_identifier="urn:li:organization:1",
                idempotency_key=idem_key,
                provider="linkedin",
                provider_message_id="urn:li:share:cached",
                status="sent",
                sent_at=datetime.now(UTC),
            )
        )

    route = respx.post(_UGC_URL).mock(
        return_value=httpx.Response(500, text="should not be called")
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        result = await tool.call(
            {
                "platform": "linkedin",
                "page_urn": "urn:li:organization:1",
                "content": {"text": "Same content"},
                "idempotency_key": idem_key,
            }
        )

    assert route.call_count == 0  # provider untouched
    assert result["provider_post_id"] == "urn:li:share:cached"
    assert result["idempotent_hit"] is True


# ---------------------------------------------------------------------------
# Media validation (E11-S05 #4)
# ---------------------------------------------------------------------------


async def test_media_required_without_url_surfaces_precondition_error(
    db_engine: AsyncEngine,
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        with pytest.raises(MediaRequiredError):
            await tool.call(
                {
                    "platform": "linkedin",
                    "page_urn": "urn:li:organization:1",
                    "content": {"text": "x", "media_required": True},
                    "idempotency_key": "k1",
                }
            )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["idempotency_key", "page_urn", "platform"],
)
async def test_missing_required_input_raises(
    db_engine: AsyncEngine, missing_field: str
) -> None:
    tenant_id = await _seed_tenant(db_engine)
    base_inputs = {
        "platform": "linkedin",
        "page_urn": "urn:li:organization:1",
        "content": {"text": "x"},
        "idempotency_key": "k",
    }
    base_inputs[missing_field] = ""

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        with pytest.raises(SocialPublishError):
            await tool.call(base_inputs)


async def test_platform_mismatch_raises(db_engine: AsyncEngine) -> None:
    """connector.provider='linkedin' but caller asked for platform='x' →
    tool refuses to dispatch a LinkedIn post as if it were an X post."""
    tenant_id = await _seed_tenant(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        with pytest.raises(SocialPublishError):
            await tool.call(
                {
                    "platform": "x",
                    "page_urn": "urn:li:organization:1",
                    "content": {"text": "x"},
                    "idempotency_key": "k",
                }
            )


# ---------------------------------------------------------------------------
# Provider error bubbling
# ---------------------------------------------------------------------------


@respx.mock
async def test_oauth_revoked_bubbles_up(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_UGC_URL).mock(
        return_value=httpx.Response(401, json={"message": "revoked"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="dead",
            session=session,
            tenant_id=tenant_id,
        )
        with pytest.raises(OAuthRevokedError):
            await tool.call(
                {
                    "platform": "linkedin",
                    "page_urn": "urn:li:organization:1",
                    "content": {"text": "x"},
                    "idempotency_key": "k-bubble",
                }
            )


@respx.mock
async def test_provider_unreachable_bubbles_up(db_engine: AsyncEngine) -> None:
    tenant_id = await _seed_tenant(db_engine)
    respx.post(_UGC_URL).mock(return_value=httpx.Response(503, text="boom"))

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, tenant_id)
        tool = SocialPublishTool(
            connector=_connector(),
            access_token="tok",
            session=session,
            tenant_id=tenant_id,
        )
        with pytest.raises(ProviderUnreachableError):
            await tool.call(
                {
                    "platform": "linkedin",
                    "page_urn": "urn:li:organization:1",
                    "content": {"text": "x"},
                    "idempotency_key": "k-503",
                }
            )

"""W43 — outbound personalisation pipeline tests.

Covers the three pure-ish layers (no live LLM, no live Apollo):
  * segmentation: members bucket correctly by seniority + title fallback.
  * enrichment: Apollo fields merge in, CSV fields take precedence.
  * generation: with an Anthropic stub, ContentAsset rows are created
    with the right metadata + merge-token usage.
  * rendering: per-contact merge fills with sensible fallbacks.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
)
from app.db.models import (
    Audience,
    AudienceMember,
    Campaign,
    ContentAsset,
    Tenant,
)
from app.integrations.apollo import ApolloClient
from app.outbound.enrichment import enrich_audience
from app.outbound.generation import generate_outreach_drafts
from app.outbound.rendering import render_personalised_drafts
from app.outbound.segmentation import segment_members


async def _seed(
    db_engine: AsyncEngine,
    *,
    member_payloads: list[dict[str, Any]],
) -> dict:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"outb-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        c = Campaign(
            tenant_id=tenant.id,
            name="Outbound Test",
            campaign_type=CampaignType.lead_gen,
            objective="Book 10 demos with mid-market ops leaders",
            brief="Outbound to ops managers + directors at SaaS scale-ups.",
            budget_total=Decimal("5000"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.audience_built,
        )
        session.add(c)
        await session.flush()
        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=c.id,
            name="Test CSV",
            segment_criteria={"source": "csv"},
            actual_size=len(member_payloads),
        )
        session.add(audience)
        await session.flush()
        for p in member_payloads:
            session.add(
                AudienceMember(
                    audience_id=audience.id,
                    external_id=p["email"],
                    payload=p,
                    source="csv",
                )
            )
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": c.id,
            "audience_id": audience.id,
        }


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def _member(**payload: Any) -> AudienceMember:
    return AudienceMember(
        audience_id=uuid.uuid4(),
        external_id=payload.get("email", uuid.uuid4().hex),
        payload=payload,
    )


def test_segmentation_buckets_by_seniority_field() -> None:
    members = [
        _member(email="a@x.test", seniority="c_suite"),
        _member(email="b@x.test", seniority="vp"),
        _member(email="c@x.test", seniority="manager"),
        _member(email="d@x.test", seniority="ic"),
    ]
    segs = segment_members(members)
    keys = [s.bucket.key for s in segs]
    assert keys == ["c_suite", "vp", "manager", "ic"]


def test_segmentation_falls_back_to_title_keywords() -> None:
    members = [
        _member(email="a@x.test", title="Chief Operating Officer"),
        _member(email="b@x.test", title="VP of Engineering"),
        _member(email="c@x.test", title="Senior Manager, Ops"),
        _member(email="d@x.test", title="Marketing Coordinator"),
    ]
    segs = segment_members(members)
    keys = {s.bucket.key for s in segs}
    assert "c_suite" in keys
    assert "vp" in keys
    assert "manager" in keys
    assert "ic" in keys


def test_segmentation_drops_empty_buckets() -> None:
    members = [_member(email="a@x.test", seniority="ic")]
    segs = segment_members(members)
    assert len(segs) == 1
    assert segs[0].bucket.key == "ic"


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


@respx.mock
async def test_enrichment_fills_missing_fields_from_apollo(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(
        db_engine,
        member_payloads=[
            {"email": "alice@acme.test", "first_name": "Alice"},
            {
                "email": "bob@acme.test",
                "first_name": "Bob",
                "title": "Director of Ops",
                "linkedin_url": "https://linkedin.com/in/bob",
                "seniority": "director",
                "company": "Acme",
            },  # already complete — should be skipped
        ],
    )

    # Apollo returns enrichment for alice only.
    respx.post("https://api.apollo.io/api/v1/people/match").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "person": {
                    "title": "VP Engineering",
                    "seniority": "vp",
                    "linkedin_url": "https://linkedin.com/in/alice",
                    "organization": {"name": "Acme", "industry": "SaaS"},
                }
            }
            if b"alice@acme.test" in req.content
            else {"person": None},
        )
    )

    client = ApolloClient(api_key="test-key")
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        summary = await enrich_audience(
            session, audience_id=world["audience_id"], apollo=client
        )

    assert summary.already_complete == 1
    assert summary.enriched == 1
    assert summary.not_found == 0
    assert summary.failed == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(AudienceMember).where(
                    AudienceMember.audience_id == world["audience_id"]
                )
            )
        ).scalars().all()
    by_email = {r.external_id: r for r in rows}
    assert by_email["alice@acme.test"].payload["title"] == "VP Engineering"
    assert by_email["alice@acme.test"].payload["first_name"] == "Alice"  # CSV wins
    # Bob's data should be untouched.
    assert by_email["bob@acme.test"].payload["title"] == "Director of Ops"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class _StubMessage:
    def __init__(self, text: str) -> None:
        from anthropic.types import TextBlock

        self.content = [TextBlock(type="text", text=text, citations=None)]


class _StubAnthropic:
    """Returns canned JSON-shaped responses. Different shape per channel
    so we can verify both code paths."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.messages = self
        self.create_count = 0

    async def create(self, **kwargs: Any) -> _StubMessage:
        prompt = kwargs["messages"][0]["content"]
        self.calls.append(prompt)
        self.create_count += 1
        if "LinkedIn connection-request" in prompt:
            body = (
                "Hi {first_name},\n\nNoticed {company} is scaling fast. "
                "We help VPs at companies like yours cut audit-prep time by "
                "60%. Worth a 15-min chat as {title}?"
            )
            return _StubMessage(json.dumps({"body": body}))
        # email
        subject = "Cutting audit prep at {company}"
        body = (
            "Hi {first_name},\n\n"
            "As {title} at {company}, you've probably felt the audit-prep "
            "crunch.\n\n"
            "We help teams like yours cut it 60% with AI agents.\n\n"
            "Worth 15 minutes next week?\n\n"
            "— MAS"
        )
        return _StubMessage(json.dumps({"subject": subject, "body": body}))


async def test_generation_creates_per_segment_assets(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(
        db_engine,
        member_payloads=[
            {"email": "ceo@acme.test", "first_name": "C", "title": "CEO", "company": "Acme"},
            {"email": "vp@acme.test", "first_name": "V", "title": "VP Eng", "company": "Acme"},
            {"email": "ic@acme.test", "first_name": "I", "title": "Eng", "company": "Acme"},
        ],
    )

    stub = _StubAnthropic()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        summary = await generate_outreach_drafts(
            session,
            campaign=c,
            audience_id=world["audience_id"],
            anthropic_client=stub,  # type: ignore[arg-type]
            model="claude-test",
        )

    # 3 segments × 2 channels = 6 LLM calls
    assert summary.segments_generated == 3
    assert summary.linkedin_assets == 3
    assert summary.email_assets == 3
    assert stub.create_count == 6

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        assets = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == world["campaign_id"]
                )
            )
        ).scalars().all()
    assert len(assets) == 6
    types = {a.asset_type for a in assets}
    assert AssetType.linkedin_dm in types
    assert AssetType.email in types
    segments = {a.extra_metadata["segment_key"] for a in assets}
    assert segments == {"c_suite", "vp", "ic"}
    for a in assets:
        # Templates must contain at least the first_name token.
        assert "{first_name}" in a.content
        assert a.status == AssetStatus.drafted


async def test_generation_skips_existing_unless_overwrite(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(
        db_engine,
        member_payloads=[{"email": "x@y.test", "title": "CEO"}],
    )
    stub = _StubAnthropic()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        first = await generate_outreach_drafts(
            session, campaign=c, audience_id=world["audience_id"],
            anthropic_client=stub, model="m",  # type: ignore[arg-type]
        )
    assert first.linkedin_assets == 1
    assert first.email_assets == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        second = await generate_outreach_drafts(
            session, campaign=c, audience_id=world["audience_id"],
            anthropic_client=stub, model="m",  # type: ignore[arg-type]
        )
    assert second.linkedin_assets == 0
    assert second.email_assets == 0
    assert second.skipped_existing == 2


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_rendering_fills_merge_tokens_per_contact(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(
        db_engine,
        member_payloads=[
            {
                "email": "ada@acme.test",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "title": "CEO",
                "company": "Acme",
                "linkedin_url": "https://linkedin.com/in/ada",
            },
            {
                "email": "bob@beta.test",
                "first_name": "Bob",
                "title": "CTO",
                "company": "Beta",
            },
        ],
    )
    stub = _StubAnthropic()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        await generate_outreach_drafts(
            session,
            campaign=c,
            audience_id=world["audience_id"],
            anthropic_client=stub,  # type: ignore[arg-type]
            model="m",
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        drafts = await render_personalised_drafts(
            session,
            campaign_id=world["campaign_id"],
            audience_id=world["audience_id"],
        )

    # 2 contacts × 2 channels = 4 drafts
    assert len(drafts) == 4
    ada = next(d for d in drafts if d.contact_email == "ada@acme.test" and d.channel == "linkedin_dm")
    assert "Hi Ada" in ada.body
    assert "Acme" in ada.body
    assert ada.contact_linkedin_url == "https://linkedin.com/in/ada"
    bob_email = next(
        d for d in drafts if d.contact_email == "bob@beta.test" and d.channel == "email"
    )
    assert bob_email.subject is not None
    assert "Beta" in bob_email.subject
    assert "Hi Bob" in bob_email.body


async def test_rendering_uses_fallback_for_missing_first_name(
    db_engine: AsyncEngine,
) -> None:
    world = await _seed(
        db_engine,
        member_payloads=[
            {"email": "nameless@x.test", "title": "Manager", "company": "X"}
        ],
    )
    stub = _StubAnthropic()
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        c = await session.get(Campaign, world["campaign_id"])
        await generate_outreach_drafts(
            session,
            campaign=c,
            audience_id=world["audience_id"],
            anthropic_client=stub,  # type: ignore[arg-type]
            model="m",
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        drafts = await render_personalised_drafts(
            session,
            campaign_id=world["campaign_id"],
            audience_id=world["audience_id"],
        )
    for d in drafts:
        assert "{first_name}" not in d.body
        assert "Hi there" in d.body

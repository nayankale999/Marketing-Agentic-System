"""W35 — A/B test dispatch substitution (E09-S02).

When a dispatch task fires for a variant that's part of a running A/B
test, only recipients whose deterministic assignment lands on THIS variant
get a send. Recipients assigned to a sibling variant are dropped (no
dispatch_attempt row); their assigned variant's own dispatch task will
handle them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.ab_testing.assignment import pick_variant_index
from app.agents.distribution import (
    dispatch_email_asset,
    schedule_approved_assets,
)
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AbTest,
    AbTestAssignment,
    Audience,
    AudienceMember,
    AppUser,
    Campaign,
    Channel,
    ContentAsset,
    DispatchAttempt,
    IntegrationCredential,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)
from app.db.session import set_tenant_context
from app.integrations.credentials import get_encrypted_payload


_SENDGRID_API = "https://api.sendgrid.com/v3/mail/send"


async def _seed_ab_world(
    db_engine: AsyncEngine, *, audience_emails: list[str]
) -> dict:
    """Tenant + email channel + campaign + 2 approved variant assets +
    running A/B test 50/50 + audience.
    """
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"absub-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        channel = Channel(
            tenant_id=tenant.id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        session.add(channel)
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@absub.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        start_date = date.today()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="absub-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=start_date,
            end_date=start_date + timedelta(days=30),
            status=CampaignStatus.approval_pending,
        )
        session.add(campaign)
        await session.flush()
        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=len(audience_emails),
            actual_size=len(audience_emails),
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()
        for em in audience_emails:
            session.add(
                AudienceMember(
                    audience_id=audience.id,
                    external_id=em,
                    payload={"email": em, "first_name": em.split("@", 1)[0]},
                    source="seed",
                    fetched_at=datetime.now(UTC),
                )
            )
        proposal = StrategyProposal(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            version=1,
            payload={
                "channels": [
                    {
                        "platform": "email",
                        "name": "Email",
                        "allocation_pct": 100,
                        "allocation_amount": "1000.00",
                        "rationale": "x",
                        "human_override": False,
                    }
                ],
                "kpis": {
                    "primary": {"metric": "mql", "target": 1, "rationale": "z"},
                    "secondary": [],
                },
            },
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()
        tp = StrategyTouchpoint(
            tenant_id=tenant.id,
            proposal_id=proposal.id,
            channel_platform="email",
            audience_id=audience.id,
            scheduled_at=datetime.combine(
                start_date + timedelta(days=1), time(9, 0), UTC
            ),
        )
        session.add(tp)
        await session.flush()

        variants = []
        for i in range(2):
            v = ContentAsset(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                asset_type=AssetType.email,
                status=AssetStatus.approved,
                title=f"Subject {i}",
                content=f"Hi {{first_name}}, variant {i}.",
                extra_metadata={
                    "channel_platform": "email",
                    "fields": {"subject": f"Subject {i}", "preheader": "Pre"},
                    "touchpoint_id": str(tp.id),
                },
                is_required=True,
            )
            session.add(v)
            await session.flush()
            variants.append(v)
        ab_test = AbTest(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="t",
            primary_metric="open",
            status=AbTestStatus.running,
            variant_a_id=variants[0].id,
            variant_b_id=variants[1].id,
            traffic_split={
                str(variants[0].id): 50,
                str(variants[1].id): 50,
            },
            started_at=datetime.now(UTC),
        )
        session.add(ab_test)
        await session.flush()
        # Link variants to the test family so the metadata lookup works.
        for v in variants:
            meta = dict(v.extra_metadata or {})
            meta["ab_test_group_id"] = str(ab_test.id)
            v.extra_metadata = meta
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "ab_test_id": ab_test.id,
            "variant_a_id": variants[0].id,
            "variant_b_id": variants[1].id,
            "emails": list(audience_emails),
        }


async def _seed_email_credential(
    db_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    payload = {
        "api_key": "sg.test-key-9999",
        "default_from_email": "alex@acme.com",
        "verified_senders": ["alex@acme.com"],
        "webhook_secret": "wh-secret",
    }
    encrypted = get_encrypted_payload().encrypt(payload)
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                channel_id=None,
                provider="sendgrid",
                label="default",
                encrypted_payload=encrypted,
                key_version=1,
            )
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_dispatch_partitions_audience_by_assigned_variant(
    db_engine: AsyncEngine,
) -> None:
    # 8 recipients gives enough headroom for 50/50 hashing to split them.
    emails = [f"user{i}@cust.com" for i in range(8)]
    world = await _seed_ab_world(db_engine, audience_emails=emails)
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-z"})
    )

    # Compute the expected partition deterministically — we use the SAME
    # function the dispatcher uses.
    split = {
        str(world["variant_a_id"]): 50,
        str(world["variant_b_id"]): 50,
    }
    expected_a = set()
    expected_b = set()
    for em in emails:
        pick = pick_variant_index(
            ab_test_id=world["ab_test_id"],
            audience_external_id=em,
            split=split,
        )
        if pick == str(world["variant_a_id"]):
            expected_a.add(em)
        else:
            expected_b.add(em)

    # Sanity: the hash should split the audience non-trivially.
    assert expected_a and expected_b, (
        f"expected hash to split audience but got {len(expected_a)}/{len(expected_b)}"
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    # Dispatch variant A.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result_a = await dispatch_email_asset(
            session, asset_id=world["variant_a_id"]
        )

    # Dispatch variant B.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result_b = await dispatch_email_asset(
            session, asset_id=world["variant_b_id"]
        )

    assert result_a["summary"]["sent"] == len(expected_a)
    assert result_b["summary"]["sent"] == len(expected_b)

    # Every recipient appears in exactly one dispatch_attempt row, on the
    # variant the hash assigned them to.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        attempts = (
            await session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.tenant_id == world["tenant_id"]
                )
            )
        ).scalars().all()
        by_email: dict[str, list[DispatchAttempt]] = {}
        for a in attempts:
            by_email.setdefault(a.recipient_identifier, []).append(a)
        for em in emails:
            assert len(by_email[em]) == 1, f"{em} got {len(by_email[em])} attempts"
            attempt = by_email[em][0]
            expected_variant = (
                world["variant_a_id"]
                if em in expected_a
                else world["variant_b_id"]
            )
            assert attempt.content_asset_id == expected_variant


@respx.mock
async def test_dispatch_assignment_is_persistent_across_retries(
    db_engine: AsyncEngine,
) -> None:
    """E09-S02 AC #1: assignment must persist across retries. The
    `ab_test_assignment` row written on first dispatch is the same one
    read on the second."""
    emails = [f"persist{i}@cust.com" for i in range(4)]
    world = await _seed_ab_world(db_engine, audience_emails=emails)
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-r"})
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    # First run.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        await dispatch_email_asset(session, asset_id=world["variant_a_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        first = {
            (a.audience_external_id, a.variant_id)
            for a in (
                await session.execute(
                    select(AbTestAssignment).where(
                        AbTestAssignment.ab_test_id == world["ab_test_id"]
                    )
                )
            ).scalars().all()
        }

    # Reset asset back to scheduled so the dispatcher accepts it again,
    # mimicking a retry that the queue handler would normally guard.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        v = await session.get(ContentAsset, world["variant_a_id"])
        v.status = AssetStatus.scheduled

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        await dispatch_email_asset(session, asset_id=world["variant_a_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        second = {
            (a.audience_external_id, a.variant_id)
            for a in (
                await session.execute(
                    select(AbTestAssignment).where(
                        AbTestAssignment.ab_test_id == world["ab_test_id"]
                    )
                )
            ).scalars().all()
        }
    assert first == second


@respx.mock
async def test_dispatch_without_ab_test_uses_full_audience(
    db_engine: AsyncEngine,
) -> None:
    """Sanity check: an asset NOT part of a running A/B test still sends
    to the full audience — A/B is opt-in per asset.
    """
    emails = [f"no-ab{i}@cust.com" for i in range(3)]
    world = await _seed_ab_world(db_engine, audience_emails=emails)
    await _seed_email_credential(db_engine, world["tenant_id"])
    respx.post(_SENDGRID_API).mock(
        return_value=httpx.Response(202, headers={"X-Message-Id": "msg-x"})
    )

    # Flip the ab_test to 'designing' — only 'running' triggers the
    # partition.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        ab = await session.get(AbTest, world["ab_test_id"])
        ab.status = AbTestStatus.designing

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await schedule_approved_assets(session, campaign=campaign)

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        result = await dispatch_email_asset(session, asset_id=world["variant_a_id"])
    assert result["summary"]["sent"] == len(emails)

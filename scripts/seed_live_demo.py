"""Seed a campaign into the live dev DB for a browser-driven walkthrough.

Different from `scripts/full_demo.py` which uses a testcontainer. This
one targets the dev compose Postgres at `localhost:5434` so you can
open the UI in a browser and see the same data.

Steps:
  1. Wipe prior demo tenant (re-runnable).
  2. Seed tenant + admin user (matching the `oidc-mock` claims).
  3. Walk a campaign through the same pipeline as full_demo.py and stop
     once the end-of-campaign report has been generated.

After this runs, open:
  http://localhost:8001/auth/login    (logs you in via oidc-mock)
  http://localhost:8001/ui/campaigns/<id>          (campaign detail)
  http://localhost:8001/ui/campaigns/<id>/report   (end-of-campaign report)

The script prints the campaign id at the end.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import register_listeners
from app.db.enums import (
    AbTestStatus,
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    EventKind,
    UserRole,
)
from app.db.models import (
    AbTest,
    AnalyticEvent,
    AppUser,
    Audience,
    AudienceMember,
    Campaign,
    Channel,
    ContentAsset,
    CustomKpi,
    DispatchAttempt,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)
from app.db.session import SessionLocal, set_tenant_context

# Must match the oidc-mock JSON_CONFIG email/hd claims so the OIDC login
# resolves to this tenant.
DEMO_TENANT_NAME = "Acme Robotics (live demo)"
DEMO_ADMIN_EMAIL = "alex@acme.test"
DEMO_HOSTED_DOMAIN = "acme.test"


def heading(t: str) -> None:
    print(f"\n── {t}")


async def _wipe_prior_demo(session: AsyncSession) -> None:
    """Drop any tenant with the same hosted domain so the script is
    re-runnable.

    audit_log + agent_log are FK'd to tenant but DON'T cascade (they're
    append-only by design). For dev DB re-runs we delete them directly
    as the owner role first, then the tenant cascade handles the rest.
    """
    result = await session.execute(
        select(Tenant.id).where(Tenant.oidc_hosted_domain == DEMO_HOSTED_DOMAIN)
    )
    tenant_ids = [tid for (tid,) in result]
    for tid in tenant_ids:
        # audit_log + agent_log: revoke restrictions apply to mas_app, but
        # the migration owner (`mas`) keeps full privileges.
        await session.execute(
            text("DELETE FROM audit_log WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        )
        await session.execute(
            text("DELETE FROM agent_log WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        )
        await session.execute(delete(Tenant).where(Tenant.id == tid))


async def seed() -> uuid.UUID:
    register_listeners()

    async with SessionLocal() as session, session.begin():
        await _wipe_prior_demo(session)

    # --- tenant + admin --------------------------------------------------
    heading("Tenant + admin")
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(
            name=DEMO_TENANT_NAME,
            oidc_hosted_domain=DEMO_HOSTED_DOMAIN,
        )
        session.add(tenant)
        await session.flush()
        admin = AppUser(
            tenant_id=tenant.id,
            email=DEMO_ADMIN_EMAIL,
            role=UserRole.admin,
            is_active=True,
            display_name="Alex (Acme Marketing)",
        )
        session.add(admin)
        await session.flush()
        tenant_id = tenant.id
        admin_id = admin.id

        email_ch = Channel(
            tenant_id=tenant_id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        linkedin_ch = Channel(
            tenant_id=tenant_id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add_all([email_ch, linkedin_ch])
        await session.flush()
        email_ch_id = email_ch.id
        linkedin_ch_id = linkedin_ch.id
    print(f"   tenant_id={tenant_id}  admin={DEMO_ADMIN_EMAIL}")

    # --- campaign + audience --------------------------------------------
    heading("Campaign + audience")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = Campaign(
            tenant_id=tenant_id,
            owner_id=admin_id,
            name="Q3 Manufacturing Buyer Push",
            campaign_type=CampaignType.demand_gen,
            objective="Drive 100 MQLs from automation/manufacturing buyers in 6 weeks",
            brief=(
                "Launch a multi-touch campaign to mid-market manufacturing CTOs "
                "highlighting our new vision-system SKU. Email + LinkedIn."
            ),
            budget_total=Decimal("25000.00"),
            currency="USD",
            start_date=date.today() - timedelta(days=12),
            end_date=date.today() + timedelta(days=30),
            kpi_targets={
                "primary": {"metric": "conversion", "target": 100},
                "secondary": [{"metric": "click", "target": 2000}],
            },
            status=CampaignStatus.live,
        )
        session.add(campaign)
        await session.flush()
        campaign_id = campaign.id

        audience = Audience(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            name="Manufacturing CTOs — North America",
            segment_criteria={
                "industry": ["Industrial Automation", "Robotics", "Manufacturing"],
                "country": ["US", "CA"],
                "title_contains": ["CTO", "VP Engineering"],
                "company_size_min": 100,
            },
            estimated_size=180,
            actual_size=160,
            refreshed_at=datetime.now(timezone.utc),
        )
        session.add(audience)
        await session.flush()
        audience_id = audience.id
        for i in range(20):
            session.add(
                AudienceMember(
                    audience_id=audience_id,
                    external_id=f"mfg-cto-{i:03d}",
                    payload={
                        "email": f"cto{i:03d}@plant-{i % 5}.demo",
                        "first_name": "Sam",
                        "company": f"Plant {i % 5} Industries",
                    },
                    source="seed",
                    fetched_at=datetime.now(timezone.utc),
                )
            )

    # --- strategy proposal + touchpoints --------------------------------
    heading("Strategy proposal")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        proposal = StrategyProposal(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            version=1,
            is_accepted=True,
            created_by_kind="agent",
            payload={
                "channels": [
                    {
                        "platform": "email",
                        "name": "Email",
                        "allocation_pct": 60,
                        "allocation_amount": "15000.00",
                        "rationale": "Direct outreach to a defined ICP.",
                        "human_override": False,
                    },
                    {
                        "platform": "linkedin",
                        "name": "LinkedIn",
                        "allocation_pct": 40,
                        "allocation_amount": "10000.00",
                        "rationale": "Awareness + retargeting.",
                        "human_override": False,
                    },
                ],
                "kpis": {
                    "primary": {"metric": "conversion", "target": 100, "rationale": "MQL"},
                    "secondary": [{"metric": "click", "target": 2000, "rationale": "engagement"}],
                },
            },
        )
        session.add(proposal)
        await session.flush()
        touchpoint_dates = [
            date.today() - timedelta(days=8),
            date.today() - timedelta(days=4),
            date.today() + timedelta(days=2),
        ]
        touchpoint_ids: list[uuid.UUID] = []
        for i, when in enumerate(touchpoint_dates):
            tp = StrategyTouchpoint(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                channel_platform="email",
                audience_id=audience_id,
                scheduled_at=datetime.combine(when, time(9, 0), timezone.utc),
                position=i + 1,
            )
            session.add(tp)
            await session.flush()
            touchpoint_ids.append(tp.id)

    # --- content assets + A/B test --------------------------------------
    heading("Content assets + running A/B test")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        variant_a = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_ch_id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="See the vision system in 90 seconds",
            content="Hi {{first_name}}, our vision SKU spots line defects 3x faster.",
            scheduled_at=datetime.combine(touchpoint_dates[0], time(9, 0), timezone.utc),
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(touchpoint_ids[0]),
                "fields": {"subject": "See the vision system in 90 seconds"},
                "variant_index": 0,
                "is_baseline": True,
            },
            is_required=True,
        )
        variant_b = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_ch_id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="Cut line-defect rates 3x — proof inside",
            content="Hi {{first_name}}, we measured 3x fewer line defects across 12 lines.",
            scheduled_at=datetime.combine(touchpoint_dates[0], time(9, 0), timezone.utc),
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(touchpoint_ids[0]),
                "fields": {"subject": "Cut line-defect rates 3x — proof inside"},
                "variant_index": 1,
                "is_baseline": False,
            },
        )
        followup = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_ch_id,
            asset_type=AssetType.email,
            status=AssetStatus.pending_approval,
            title="One more thought on vision systems",
            content="Hi {{first_name}}, sharing a case study — 28% throughput lift.",
            scheduled_at=datetime.combine(touchpoint_dates[1], time(9, 0), timezone.utc),
            extra_metadata={"channel_platform": "email", "touchpoint_id": str(touchpoint_ids[1])},
        )
        linkedin_post = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=linkedin_ch_id,
            asset_type=AssetType.social_post,
            status=AssetStatus.approved,
            title="LinkedIn touchpoint",
            content="3x fewer line defects, 28% more throughput. See how →",
            scheduled_at=datetime.combine(touchpoint_dates[2], time(11, 0), timezone.utc),
            extra_metadata={"channel_platform": "linkedin", "touchpoint_id": str(touchpoint_ids[2])},
        )
        session.add_all([variant_a, variant_b, followup, linkedin_post])
        await session.flush()
        variant_a_id = variant_a.id
        variant_b_id = variant_b.id

        ab = AbTest(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            name="Subject A vs B — vision system",
            primary_metric="open",
            status=AbTestStatus.running,
            variant_a_id=variant_a_id,
            variant_b_id=variant_b_id,
            traffic_split={str(variant_a_id): 50, str(variant_b_id): 50},
            min_runtime_hours=24,
            max_runtime_hours=168,
            started_at=datetime.now(timezone.utc) - timedelta(days=6),
        )
        session.add(ab)
        await session.flush()
        ab_id = ab.id

    # --- dispatch + events ----------------------------------------------
    heading("Dispatch + analytics events")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        from app.ab_testing.assignment import assign_variant

        now = datetime.now(timezone.utc)
        for i in range(20):
            ext = f"mfg-cto-{i:03d}"
            email = f"cto{i:03d}@plant-{i % 5}.demo"
            assigned = await assign_variant(
                session,
                tenant_id=tenant_id,
                ab_test_id=ab_id,
                audience_external_id=ext,
            )
            sg_msg = f"sg-{ext}"
            session.add(
                DispatchAttempt(
                    tenant_id=tenant_id,
                    content_asset_id=assigned,
                    audience_external_id=ext,
                    recipient_identifier=email,
                    idempotency_key=f"k-{ext}",
                    provider="sendgrid",
                    provider_message_id=sg_msg,
                    status="sent",
                    sent_at=now - timedelta(days=5),
                )
            )
            on_b = assigned == variant_b_id
            if on_b or i % 3 == 0:
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                    event_type=EventKind.open, payload={"sg_message_id": sg_msg},
                    provider_event_id=f"o-{ext}",
                    event_at=now - timedelta(days=5, hours=-i),
                ))
            if on_b and i % 2 == 0:
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                    event_type=EventKind.click, payload={"sg_message_id": sg_msg, "utm_content": "demo"},
                    provider_event_id=f"c-{ext}",
                    event_at=now - timedelta(days=5, hours=-i),
                ))
            if on_b and i % 3 == 0:
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                    event_type=EventKind.conversion, payload={"sg_message_id": sg_msg},
                    provider_event_id=f"v-{ext}",
                    event_at=now - timedelta(days=4, hours=-i),
                ))

        # Per-channel spend.
        session.add(AnalyticEvent(
            tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
            event_type=EventKind.spend, metric_value=Decimal("4200.00"), payload={},
            event_at=now - timedelta(days=1),
        ))
        session.add(AnalyticEvent(
            tenant_id=tenant_id, campaign_id=campaign_id, channel_id=linkedin_ch_id,
            event_type=EventKind.spend, metric_value=Decimal("6800.00"), payload={},
            event_at=now - timedelta(days=1),
        ))
        for _ in range(15):
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=linkedin_ch_id,
                event_type=EventKind.click, payload={},
                provider_event_id=f"li-{uuid.uuid4().hex[:10]}",
                event_at=now - timedelta(days=3),
            ))
        for _ in range(2):
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=linkedin_ch_id,
                event_type=EventKind.conversion, payload={},
                provider_event_id=f"li-conv-{uuid.uuid4().hex[:10]}",
                event_at=now - timedelta(days=3),
            ))

    # --- anomaly baseline + spike (so the analytics agent has something) -
    heading("Anomaly baseline + spike")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        baseline_start = datetime.now(timezone.utc) - timedelta(days=13)
        for d in range(13):
            day = baseline_start + timedelta(days=d)
            for _ in range(2):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                    event_type=EventKind.unsubscribe, payload={},
                    provider_event_id=f"base-{uuid.uuid4().hex[:10]}",
                    event_at=day,
                ))
        for _ in range(60):
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                event_type=EventKind.unsubscribe, payload={},
                provider_event_id=f"spike-{uuid.uuid4().hex[:10]}",
                event_at=datetime.now(timezone.utc) - timedelta(hours=4),
            ))

    # --- run analytics: anomaly, recommendation, custom KPI -------------
    heading("Analytics & Optimisation pass")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        from app.analytics.anomaly import detect_anomalies
        from app.analytics.recommendations import generate_recommendations
        from app.ab_testing.significance import evaluate_test
        from app.analytics.spend_reconciliation import (
            ChannelSpend,
            ingest_platform_spend,
            run_reconciliation,
        )

        await evaluate_test(session, ab_test_id=ab_id, now=datetime.now(timezone.utc))
        anomalies = await detect_anomalies(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
        recs = await generate_recommendations(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
        await ingest_platform_spend(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            records=[
                ChannelSpend(channel_id=email_ch_id, amount=Decimal("4200.00")),
                ChannelSpend(channel_id=linkedin_ch_id, amount=Decimal("6800.00")),
            ],
        )
        await run_reconciliation(
            session, tenant_id=tenant_id,
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            invoices={campaign_id: Decimal("11330.00")},
        )

        custom = CustomKpi(
            tenant_id=tenant_id, campaign_id=campaign_id,
            name="demo_clicks_7d",
            formula={
                "event_type": "click",
                "filters": [{"path": "payload.utm_content", "op": "eq", "value": "demo"}],
                "window_days": 7,
            },
            created_by=admin_id,
        )
        session.add(custom)
    print(f"   anomalies={len(anomalies)}  recommendations={len(recs)}")

    # --- generate the report -------------------------------------------
    heading("End-of-campaign report")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        from app.analytics.report import generate_report

        report = await generate_report(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
            generated_by=str(admin_id),
        )
    print(f"   report v{report.version}  is_latest={report.is_latest}")

    return campaign_id


async def main() -> None:
    campaign_id = await seed()
    print("\n" + "═" * 78)
    print(f"  Demo data seeded.")
    print(f"  campaign_id = {campaign_id}")
    print("═" * 78)
    print("\n  Open in your browser (after starting the app):")
    print(f"    http://localhost:8001/auth/login")
    print(f"    http://localhost:8001/ui/campaigns/{campaign_id}")
    print(f"    http://localhost:8001/ui/campaigns/{campaign_id}/report")
    print(f"    http://localhost:8001/ui/approvals")
    print(
        "\n  Start the app:\n"
        "    uv run uvicorn app.api.app:app --host 0.0.0.0 --port 8001 --reload\n"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)

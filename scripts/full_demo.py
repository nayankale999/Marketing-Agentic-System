"""End-to-end MAS demo (W1 -> W41).

Boots a fresh Postgres in a testcontainer, runs every Alembic migration,
then walks one campaign through every agent — Audience Targeting,
Strategist, Content Creator, Approval, Distribution, Analytics &
Optimisation — printing what each layer wrote.

Why testcontainer: this script doubles as a recording for the marketing
video. We don't want it to depend on a dev compose stack being healthy
or on whatever historical state is sitting in the local Postgres.

Run:    .venv/bin/python -m scripts.full_demo
Prereq: docker daemon running. No other setup.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

DASH = "─" * 78
DEMO_TENANT_NAME = "Northwind Robotics (MAS demo)"


def heading(title: str) -> None:
    print(f"\n{DASH}\n  {title}\n{DASH}")


def step(label: str, body: str | dict[str, Any] | list[Any] | None = None) -> None:
    print(f"\n• {label}")
    if body is None:
        return
    if isinstance(body, str):
        for line in body.splitlines():
            print(f"    {line}")
    else:
        import json

        print(
            "    "
            + json.dumps(body, indent=2, default=str).replace("\n", "\n    ")
        )


async def run_demo() -> None:
    # --- 1. Boot Postgres + apply migrations ----------------------------
    heading("1. Boot isolated Postgres + apply 22 migrations")
    with PostgresContainer("postgres:15-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        os.environ["DATABASE_URL"] = url
        # Force get_settings() to re-read the env var.
        from app.settings.config import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]

        # Run migrations BEFORE importing the rest — env.py uses
        # DATABASE_URL so the schema lands in our testcontainer.
        command.upgrade(Config("alembic.ini"), "head")
        step("Postgres ready", {"url": url[: url.find("@") + 1] + "<host>:<port>/test"})

        await _walk_through_pipeline()


async def _walk_through_pipeline() -> None:
    # Imports happen here so they pick up the testcontainer DATABASE_URL.
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select, func

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
        CampaignChannelBudget,
        Channel,
        ContentAsset,
        CustomKpi,
        DispatchAttempt,
        MetricAnomaly,
        OptimisationRecommendation,
        Tenant,
    )
    from app.db.session import SessionLocal, set_tenant_context
    from app.analytics.anomaly import detect_anomalies
    from app.analytics.custom_kpis import evaluate_custom_kpi
    from app.analytics.kpi_rollup import compute_campaign_kpis
    from app.analytics.recommendations import generate_recommendations
    from app.analytics.report import generate_report
    from app.analytics.spend_reconciliation import (
        ChannelSpend,
        ingest_platform_spend,
        run_reconciliation,
    )

    register_listeners()

    # --- 2. Tenant + admin user -----------------------------------------
    heading("2. Seed tenant + admin (the marketing team)")
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(name=DEMO_TENANT_NAME)
        session.add(tenant)
        await session.flush()
        admin = AppUser(
            tenant_id=tenant.id,
            email="marketing@northwind.demo",
            role=UserRole.admin,
            is_active=True,
            display_name="Northwind Marketing",
        )
        session.add(admin)
        await session.flush()
        tenant_id = tenant.id
        admin_id = admin.id

        # Channels we'll publish through.
        email_channel = Channel(
            tenant_id=tenant_id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        linkedin_channel = Channel(
            tenant_id=tenant_id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add_all([email_channel, linkedin_channel])
        await session.flush()
        email_channel_id = email_channel.id
        linkedin_channel_id = linkedin_channel.id

    step(
        "Tenant + admin + channels created",
        {
            "tenant_id": str(tenant_id),
            "admin": "marketing@northwind.demo (admin)",
            "channels": ["Email", "LinkedIn"],
        },
    )

    # --- 3. Campaign brief ---------------------------------------------
    heading("3. Marketer drafts campaign brief")
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
                "highlighting our new vision-system SKU. Email + LinkedIn. "
                "Primary KPI: MQLs. Secondary: demo bookings."
            ),
            budget_total=Decimal("25000.00"),
            currency="USD",
            start_date=date.today() - timedelta(days=12),
            end_date=date.today() + timedelta(days=30),
            kpi_targets={
                "primary": {"metric": "conversion", "target": 100},
                "secondary": [{"metric": "click", "target": 2000}],
            },
            status=CampaignStatus.audience_built,
        )
        session.add(campaign)
        await session.flush()
        campaign_id = campaign.id
    step(
        "Campaign created",
        {
            "name": "Q3 Manufacturing Buyer Push",
            "objective": "100 MQLs in 6 weeks",
            "budget": "$25,000.00 USD",
            "channels": "Email + LinkedIn",
        },
    )

    # --- 4. Audience materialisation -----------------------------------
    heading("4. Audience Targeting agent materialises the ICP")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
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
        # Synthetic audience members (small N for the demo).
        for i in range(20):
            session.add(
                AudienceMember(
                    audience_id=audience.id,
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
        audience_id = audience.id
    step(
        "Audience materialised",
        {
            "name": "Manufacturing CTOs — North America",
            "estimated_size": 180,
            "actual_size": 160,
            "members_demoed": 20,
        },
    )

    # --- 5. Strategist proposal ----------------------------------------
    heading("5. Strategist agent proposes a channel mix + calendar")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        from app.db.models import StrategyProposal, StrategyTouchpoint

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
                        "rationale": "Direct outreach to a defined ICP — highest predicted CPL.",
                        "human_override": False,
                    },
                    {
                        "platform": "linkedin",
                        "name": "LinkedIn",
                        "allocation_pct": 40,
                        "allocation_amount": "10000.00",
                        "rationale": "Awareness + retargeting against the same ICP.",
                        "human_override": False,
                    },
                ],
                "kpis": {
                    "primary": {"metric": "conversion", "target": 100, "rationale": "MQL"},
                    "secondary": [
                        {"metric": "click", "target": 2000, "rationale": "engagement"}
                    ],
                },
                "ab_tests": [{"channel": "email", "variants": 2}],
            },
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
        # Three touchpoints across the campaign window.
        touchpoint_dates = [
            date.today() - timedelta(days=8),
            date.today() - timedelta(days=4),
            date.today() + timedelta(days=2),
        ]
        touchpoint_ids: list[uuid.UUID] = []
        for i, when in enumerate(touchpoint_dates):
            tp = StrategyTouchpoint(
                tenant_id=tenant_id,
                proposal_id=proposal_id,
                channel_platform="email",
                audience_id=audience_id,
                scheduled_at=datetime.combine(when, time(9, 0), timezone.utc),
                position=i + 1,
            )
            session.add(tp)
            await session.flush()
            touchpoint_ids.append(tp.id)
    step(
        "Strategy accepted",
        {
            "split": "Email 60% / LinkedIn 40%",
            "touchpoints": 3,
            "ab_test": "2-variant subject line on touchpoint 1",
        },
    )

    # --- 6. Content generation + approval ------------------------------
    heading("6. Content Creator drafts assets; manager approves")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        # Two variants on touchpoint 1 (A/B baseline), one asset per other tp.
        variant_a = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_channel_id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="See the vision system in 90 seconds",
            content=(
                "Hi {{first_name}}, our new vision SKU spots line defects 3x "
                "faster than current options. Want a 15-minute walkthrough?"
            ),
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(touchpoint_ids[0]),
                "fields": {"subject": "See the vision system in 90 seconds"},
                "variant_index": 0,
                "is_baseline": True,
            },
            scheduled_at=datetime.combine(touchpoint_dates[0], time(9, 0), timezone.utc),
            is_required=True,
        )
        variant_b = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_channel_id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="Cut line-defect rates 3x — proof inside",
            content=(
                "Hi {{first_name}}, we measured 3x fewer line defects "
                "across 12 production lines. Quick demo this week?"
            ),
            extra_metadata={
                "channel_platform": "email",
                "touchpoint_id": str(touchpoint_ids[0]),
                "fields": {"subject": "Cut line-defect rates 3x — proof inside"},
                "variant_index": 1,
                "is_baseline": False,
            },
            scheduled_at=datetime.combine(touchpoint_dates[0], time(9, 0), timezone.utc),
            is_required=False,
        )
        session.add_all([variant_a, variant_b])
        await session.flush()
        variant_a_id = variant_a.id
        variant_b_id = variant_b.id

        # Touchpoint 2 + 3 follow-ups.
        followup_2 = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=email_channel_id,
            asset_type=AssetType.email,
            status=AssetStatus.approved,
            title="One more thought on vision systems",
            content="Hi {{first_name}}, sharing a case study — 28% throughput lift.",
            scheduled_at=datetime.combine(touchpoint_dates[1], time(9, 0), timezone.utc),
            extra_metadata={"channel_platform": "email", "touchpoint_id": str(touchpoint_ids[1])},
        )
        followup_3 = ContentAsset(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            channel_id=linkedin_channel_id,
            asset_type=AssetType.social_post,
            status=AssetStatus.approved,
            title="LinkedIn touchpoint",
            content="3x fewer line defects, 28% more throughput. See how →",
            scheduled_at=datetime.combine(touchpoint_dates[2], time(11, 0), timezone.utc),
            extra_metadata={
                "channel_platform": "linkedin",
                "touchpoint_id": str(touchpoint_ids[2]),
            },
        )
        session.add_all([followup_2, followup_3])
        await session.flush()
    step(
        "Content drafted + approved",
        {
            "assets": 4,
            "ab_variants": "subject A: 'See the vision system in 90 seconds' / subject B: 'Cut line-defect rates 3x'",
            "approver": "marketing@northwind.demo",
        },
    )

    # --- 7. A/B test definition + launch -------------------------------
    heading("7. A/B test defined + launched")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
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
    step(
        "A/B test running",
        {"split": "50/50", "min_runtime": "24h", "max_runtime": "168h"},
    )

    # --- 8. Synthetic dispatch + analytics ingest ----------------------
    heading("8. Dispatch traffic + ingest webhook / analytics events")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        now = datetime.now(timezone.utc)
        # For each audience member, deterministically assign a variant via
        # the W35 assignment helper, write a DispatchAttempt, and emit a
        # few analytic_events that look like the email connector wrote them.
        from app.ab_testing.assignment import assign_variant

        for i in range(20):
            external_id = f"mfg-cto-{i:03d}"
            email = f"cto{i:03d}@plant-{i % 5}.demo"
            assigned_variant = await assign_variant(
                session,
                tenant_id=tenant_id,
                ab_test_id=ab_id,
                audience_external_id=external_id,
            )
            sg_msg = f"sg-{external_id}"
            session.add(
                DispatchAttempt(
                    tenant_id=tenant_id,
                    content_asset_id=assigned_variant,
                    audience_external_id=external_id,
                    recipient_identifier=email,
                    idempotency_key=f"k-{external_id}",
                    provider="sendgrid",
                    provider_message_id=sg_msg,
                    status="sent",
                    sent_at=now - timedelta(days=5),
                )
            )
            # Simulate engagement biased toward variant B (which will become
            # the winner). 1 open per recipient max so x ≤ n holds for the
            # proportion test.
            on_b = assigned_variant == variant_b_id
            opens = 1 if (on_b or i % 3 == 0) else 0  # ~95% opens on B, ~33% on A
            clicks = 1 if (on_b and i % 2 == 0) else 0
            convs = 1 if (on_b and i % 3 == 0) else 0
            for _ in range(opens):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        campaign_id=campaign_id,
                        channel_id=email_channel_id,
                        event_type=EventKind.open,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"evt-open-{external_id}-{uuid.uuid4().hex[:6]}",
                        event_at=now - timedelta(days=5, hours=-i),
                    )
                )
            for _ in range(clicks):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        campaign_id=campaign_id,
                        channel_id=email_channel_id,
                        event_type=EventKind.click,
                        payload={"sg_message_id": sg_msg, "utm_content": "demo"},
                        provider_event_id=f"evt-click-{external_id}-{uuid.uuid4().hex[:6]}",
                        event_at=now - timedelta(days=5, hours=-i),
                    )
                )
            for _ in range(convs):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        campaign_id=campaign_id,
                        channel_id=email_channel_id,
                        event_type=EventKind.conversion,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"evt-conv-{external_id}-{uuid.uuid4().hex[:6]}",
                        event_at=now - timedelta(days=4, hours=-i),
                    )
                )
        # Per-channel spend.
        session.add(
            AnalyticEvent(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                channel_id=email_channel_id,
                event_type=EventKind.spend,
                metric_value=Decimal("4200.00"),
                payload={},
                event_at=now - timedelta(days=1),
            )
        )
        session.add(
            AnalyticEvent(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                channel_id=linkedin_channel_id,
                event_type=EventKind.spend,
                metric_value=Decimal("6800.00"),
                payload={},
                event_at=now - timedelta(days=1),
            )
        )
        # LinkedIn clicks + a couple of conversions — significantly worse
        # cost-per-outcome than email so the recommendation engine proposes
        # shifting budget. Both channels need >0 conversions for the rule
        # to engage on the conversion outcome.
        for i in range(15):
            session.add(
                AnalyticEvent(
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    channel_id=linkedin_channel_id,
                    event_type=EventKind.click,
                    payload={},
                    provider_event_id=f"li-click-{uuid.uuid4().hex[:10]}",
                    event_at=now - timedelta(days=3),
                )
            )
        for i in range(2):
            session.add(
                AnalyticEvent(
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    channel_id=linkedin_channel_id,
                    event_type=EventKind.conversion,
                    payload={},
                    provider_event_id=f"li-conv-{uuid.uuid4().hex[:10]}",
                    event_at=now - timedelta(days=3),
                )
            )
    step(
        "Dispatch + analytics seeded",
        {
            "dispatch_attempts": 20,
            "events": "opens / clicks / conversions / spend",
            "skew": "variant B engagement biased — will become the winner",
        },
    )

    # --- 9. KPI rollup -------------------------------------------------
    heading("9. Real-time KPI snapshot (W34)")
    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        snap = await compute_campaign_kpis(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
    kpi_dict = snap.kpis.as_dict()
    step(
        "Campaign KPIs",
        {
            "impressions": kpi_dict["impressions"],
            "opens": kpi_dict["opens"],
            "clicks": kpi_dict["clicks"],
            "conversions": kpi_dict["conversions"],
            "spend": kpi_dict["spend"],
            "ctr": kpi_dict["derived"]["ctr"],
            "open_rate": kpi_dict["derived"]["open_rate"],
        },
    )

    # --- 10. A/B significance + winner ---------------------------------
    heading("10. ab.testing tool computes significance + sets winner (W36)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        from app.ab_testing.significance import evaluate_test

        eval_result = await evaluate_test(
            session,
            ab_test_id=ab_id,
            now=datetime.now(timezone.utc),
        )
    step(
        "A/B evaluation",
        {
            "decision": eval_result.decision,
            "p_value": f"{eval_result.p_value:.4f}" if eval_result.p_value else None,
            "lift": f"{eval_result.lift:.4f}" if eval_result.lift else None,
            "sample_a (n, x)": eval_result.sample_a,
            "sample_b (n, x)": eval_result.sample_b,
            "winner_id": str(eval_result.winner_id) if eval_result.winner_id else None,
        },
    )

    # --- 11. Anomaly detection + recommendation ------------------------
    heading("11. Analytics & Optimisation agent: anomalies + recommendation (W37/W39)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        # Seed a tame baseline + 1 spike day. detect_anomalies uses a
        # 14-day window and needs len(series) >= 14 distinct days (13
        # baseline + 1 observation). We seed exactly the days inside
        # the cutoff so the latest day is the spike, not a baseline day.
        baseline_start = datetime.now(timezone.utc) - timedelta(days=13)
        for d in range(13):
            day = baseline_start + timedelta(days=d)
            for _ in range(2):
                session.add(
                    AnalyticEvent(
                        tenant_id=tenant_id,
                        campaign_id=campaign_id,
                        channel_id=email_channel_id,
                        event_type=EventKind.unsubscribe,
                        payload={},
                        provider_event_id=f"base-{uuid.uuid4().hex[:10]}",
                        event_at=day,
                    )
                )
        # Spike on day 14: 60 unsubscribes (way above the 2/day baseline)
        for _ in range(60):
            session.add(
                AnalyticEvent(
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    channel_id=email_channel_id,
                    event_type=EventKind.unsubscribe,
                    payload={},
                    provider_event_id=f"spike-{uuid.uuid4().hex[:10]}",
                    event_at=datetime.now(timezone.utc) - timedelta(hours=4),
                )
            )

    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        anomalies = await detect_anomalies(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
    step(
        "Anomalies detected",
        [
            {
                "metric": a.metric,
                "severity": a.severity,
                "sigma": str(a.sigma),
                "observed": str(a.observed_value),
                "baseline_median": str(a.baseline_median),
            }
            for a in anomalies
        ]
        or "(none)",
    )

    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        recs = await generate_recommendations(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
    step(
        "Optimisation recommendations",
        [
            {
                "kind": r.kind,
                "rationale": r.rationale,
                "predicted_uplift": str(r.predicted_uplift),
                "proposal": r.proposal,
            }
            for r in recs
        ]
        or "(none)",
    )

    # --- 12. Custom KPI evaluation -------------------------------------
    heading("12. Custom KPI: 'demo clicks within 7 days' (W41)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        custom = CustomKpi(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            name="demo_clicks_7d",
            formula={
                "event_type": "click",
                "filters": [
                    {"path": "payload.utm_content", "op": "eq", "value": "demo"}
                ],
                "window_days": 7,
            },
            created_by=admin_id,
        )
        session.add(custom)
        await session.flush()
        custom_id = custom.id

    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        custom_loaded = await session.get(CustomKpi, custom_id)
        custom_result = await evaluate_custom_kpi(
            session,
            kpi=custom_loaded,
            campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
        )
    step(
        "Custom KPI evaluated",
        {
            "name": "demo_clicks_7d",
            "value": custom_result.value,
            "missing_event": custom_result.missing_event,
        },
    )

    # --- 13. Spend ingest + reconciliation ------------------------------
    heading("13. Spend ingest + monthly reconciliation (W39/W41)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        await ingest_platform_spend(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            records=[
                ChannelSpend(channel_id=email_channel_id, amount=Decimal("4200.00")),
                ChannelSpend(channel_id=linkedin_channel_id, amount=Decimal("6800.00")),
            ],
        )

    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        recon_rows = await run_reconciliation(
            session,
            tenant_id=tenant_id,
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            # Pretend the platform invoice came in 3% high (drift).
            invoices={campaign_id: Decimal("11330.00")},
        )
    step(
        "Spend reconciliation",
        [
            {
                "period": f"{r.period_start.isoformat()} → {r.period_end.isoformat()}",
                "committed": str(r.committed_amount),
                "invoiced": str(r.invoiced_amount),
                "delta_pct": str(r.delta_pct),
                "status": r.status,
            }
            for r in recon_rows
        ],
    )

    # --- 14. End-of-campaign report -------------------------------------
    heading("14. End-of-campaign report generated (W38)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        report = await generate_report(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            now=datetime.now(timezone.utc),
            generated_by="system",
        )
    step(
        "Report v1 written",
        {
            "version": report.version,
            "is_latest": report.is_latest,
            "sections": list(report.data.keys()),
        },
    )

    # --- 15. Summary ----------------------------------------------------
    heading("15. End-to-end pipeline complete")
    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        totals: dict[str, int] = {}
        for label, model in (
            ("dispatch_attempts", DispatchAttempt),
            ("analytic_events", AnalyticEvent),
            ("metric_anomalies", MetricAnomaly),
            ("recommendations", OptimisationRecommendation),
            ("content_assets", ContentAsset),
            ("campaign_channel_budget_rows", CampaignChannelBudget),
        ):
            n = (
                await session.execute(
                    select(func.count()).select_from(model)
                )
            ).scalar_one()
            totals[label] = int(n)
    step("Tenant row counts", totals)
    print(
        "\n  MAS walked one campaign from brief → audience → strategy → content →\n"
        "  approval → dispatch → analytics → optimisation → report in a single run.\n"
    )


def main() -> int:
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

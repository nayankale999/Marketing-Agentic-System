"""Finalise the Provision 29 2026 demo campaign.

Steps:
  1. Launch the campaign (ready_to_launch → live)
  2. Seed realistic dispatch_attempt + analytic_event rows so the KPI
     panel + report have meaningful numbers
  3. Run the analytics agent (anomaly + recommendation passes)
  4. Generate the end-of-campaign report

After this, the campaign has a full closing artifact in the dashboard.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.analytics.anomaly import detect_anomalies
from app.analytics.kpi_rollup import compute_campaign_kpis
from app.analytics.recommendations import generate_recommendations
from app.analytics.report import generate_report
from app.analytics.spend_reconciliation import (
    ChannelSpend,
    ingest_platform_spend,
    run_reconciliation,
)
from app.assistant.tools import launch_campaign
from app.db.enums import (
    AssetStatus,
    CampaignStatus,
    EventKind,
    UserRole,
)
from app.db.models import (
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
)
from app.db.session import SessionLocal, set_tenant_context


CAMPAIGN_NAME = "Provision 29 2026"


def heading(t: str) -> None:
    print(f"\n{'─' * 78}\n  {t}\n{'─' * 78}")


def step(label: str, body=None) -> None:
    print(f"\n• {label}")
    if body is None:
        return
    if isinstance(body, str):
        for line in body.splitlines():
            print(f"    {line}")
    else:
        import json

        print("    " + json.dumps(body, indent=2, default=str).replace("\n", "\n    "))


async def _pick_manager(session, tenant_id) -> AppUser:
    row = (
        await session.execute(
            select(AppUser)
            .where(
                AppUser.tenant_id == tenant_id,
                AppUser.role.in_([UserRole.admin, UserRole.manager]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    return (
        await session.execute(
            select(AppUser).where(AppUser.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one()


async def run() -> int:
    # --- Locate campaign ------------------------------------------------
    async with SessionLocal() as session:
        c = (
            await session.execute(
                select(Campaign).where(Campaign.name == CAMPAIGN_NAME)
            )
        ).scalar_one_or_none()
        if c is None:
            print(f"ERROR: no campaign named '{CAMPAIGN_NAME}' found.")
            return 2
        tenant_id = c.tenant_id
        campaign_id = c.id

    # --- 1. Launch ------------------------------------------------------
    heading("1. Launch the campaign")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        manager = await _pick_manager(session, tenant_id)
        c = await session.get(Campaign, campaign_id)
        if c.status == CampaignStatus.live:
            step("Already live.")
        else:
            result = await launch_campaign(
                session, user=manager, campaign=str(campaign_id), confirm=True
            )
            step(result.summary, result.data)

    # --- 2. Seed dispatch + engagement ---------------------------------
    heading("2. Seed dispatch traffic + engagement events")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)

        # Pick the audience + recipients
        audience = (
            await session.execute(
                select(Audience).where(Audience.campaign_id == campaign_id).limit(1)
            )
        ).scalar_one_or_none()
        if audience is None:
            print("ERROR: campaign has no audience.")
            return 2

        members = (
            await session.execute(
                select(AudienceMember).where(AudienceMember.audience_id == audience.id)
            )
        ).scalars().all()

        # Pick the approved assets, partition by channel.
        assets = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == campaign_id,
                    ContentAsset.status == AssetStatus.approved,
                )
            )
        ).scalars().all()
        email_assets = [a for a in assets if a.channel_id is not None]
        if not email_assets:
            email_assets = assets[:3]

        # Build channel lookup so we can attribute events with channel_id.
        channels = (
            await session.execute(
                select(Channel).where(Channel.tenant_id == tenant_id)
            )
        ).scalars().all()
        ch_by_id: dict = {c.id: c for c in channels}

        now = datetime.now(UTC)
        n_attempts = 0
        # Use the first 3 assets across N recipients each, with realistic
        # engagement rates per (email open ≈ 40%, click ≈ 18%, conv ≈ 4%).
        primary_assets = email_assets[:3] if len(email_assets) >= 3 else email_assets
        for asset in primary_assets:
            for i, m in enumerate(members):
                ext = m.external_id or f"unknown-{i}"
                email = (m.payload or {}).get("email") or f"u{i}@demo.test"
                sg_msg = f"sg-{asset.id.hex[:6]}-{i}"
                session.add(
                    DispatchAttempt(
                        tenant_id=tenant_id,
                        content_asset_id=asset.id,
                        audience_external_id=ext,
                        recipient_identifier=email,
                        idempotency_key=f"k-{asset.id.hex[:6]}-{i}",
                        provider="sendgrid",
                        provider_message_id=sg_msg,
                        status="sent",
                        sent_at=now - timedelta(days=2, hours=-i),
                    )
                )
                n_attempts += 1
                if i % 5 != 4:  # ~80% impressions register
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id,
                        channel_id=asset.channel_id,
                        event_type=EventKind.impression,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"i-{asset.id.hex[:6]}-{i}",
                        event_at=now - timedelta(days=2, hours=-i),
                    ))
                if i % 5 < 2:  # ~40% open rate
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id,
                        channel_id=asset.channel_id,
                        event_type=EventKind.open,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"o-{asset.id.hex[:6]}-{i}",
                        event_at=now - timedelta(days=2, hours=-i - 1),
                    ))
                if i % 5 == 0:  # ~20% CTR
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id,
                        channel_id=asset.channel_id,
                        event_type=EventKind.click,
                        payload={"sg_message_id": sg_msg, "utm_content": "demo"},
                        provider_event_id=f"c-{asset.id.hex[:6]}-{i}",
                        event_at=now - timedelta(days=2, hours=-i - 2),
                    ))
                if i % 10 == 0:  # ~10% conversion
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id,
                        channel_id=asset.channel_id,
                        event_type=EventKind.conversion,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"v-{asset.id.hex[:6]}-{i}",
                        event_at=now - timedelta(days=1, hours=-i),
                    ))

        # Per-channel spend.
        for ch in channels:
            spend_amt = Decimal("2400.00") if "Email" in ch.name else Decimal("3600.00")
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=ch.id,
                event_type=EventKind.spend, metric_value=spend_amt,
                payload={}, event_at=now - timedelta(days=1),
            ))

        # LinkedIn-ish baseline engagement (a few clicks + 1 conversion) so
        # the budget-shift recommendation has both channels with data.
        linkedin_ch = next(
            (c for c in channels if "LinkedIn" in c.name), None
        )
        if linkedin_ch is not None:
            for j in range(8):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id,
                    channel_id=linkedin_ch.id,
                    event_type=EventKind.click, payload={},
                    provider_event_id=f"li-c-{uuid.uuid4().hex[:8]}",
                    event_at=now - timedelta(days=1),
                ))
            for _ in range(1):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id,
                    channel_id=linkedin_ch.id,
                    event_type=EventKind.conversion, payload={},
                    provider_event_id=f"li-v-{uuid.uuid4().hex[:8]}",
                    event_at=now - timedelta(days=1),
                ))

    step("Seeded", {"dispatch_attempts": n_attempts, "assets_used": len(primary_assets)})

    # --- 3. Anomaly baseline + spike (so the report has anomalies) -----
    heading("3. Seed anomaly baseline + 3σ spike")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        baseline_start = datetime.now(UTC) - timedelta(days=13)
        for d in range(13):
            day = baseline_start + timedelta(days=d)
            for _ in range(2):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id,
                    channel_id=None,
                    event_type=EventKind.unsubscribe, payload={},
                    provider_event_id=f"base-{uuid.uuid4().hex[:10]}",
                    event_at=day,
                ))
        for _ in range(45):
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id,
                channel_id=None,
                event_type=EventKind.unsubscribe, payload={},
                provider_event_id=f"spike-{uuid.uuid4().hex[:10]}",
                event_at=datetime.now(UTC) - timedelta(hours=4),
            ))

    # --- 4. Analytics pass ---------------------------------------------
    heading("4. Analytics & Optimisation pass")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        anomalies = await detect_anomalies(
            session, tenant_id=tenant_id, campaign_id=campaign_id, now=datetime.now(UTC)
        )
        recs = await generate_recommendations(
            session, tenant_id=tenant_id, campaign_id=campaign_id, now=datetime.now(UTC)
        )
    step("Anomalies + recommendations", {
        "anomalies": [{"metric": a.metric, "severity": a.severity, "sigma": str(a.sigma)} for a in anomalies],
        "recommendations": [{"kind": r.kind, "predicted_uplift": str(r.predicted_uplift)} for r in recs],
    })

    # --- 5. Spend ingest + reconciliation ------------------------------
    heading("5. Spend reconciliation")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        channels = (await session.execute(select(Channel).where(Channel.tenant_id == tenant_id))).scalars().all()
        spend_records = []
        for ch in channels:
            spend_amt = Decimal("2400.00") if "Email" in ch.name else Decimal("3600.00")
            spend_records.append(ChannelSpend(channel_id=ch.id, amount=spend_amt))
        await ingest_platform_spend(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            records=spend_records,
        )
        # Pretend the invoice came in 2.5% high.
        await run_reconciliation(
            session, tenant_id=tenant_id,
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            invoices={campaign_id: Decimal("6150.00")},
        )
    step("Reconciliation written")

    # --- 6. Custom KPI -------------------------------------------------
    heading("6. Custom KPI (compliance-readers who clicked through)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        # Skip if already exists.
        existing = (await session.execute(select(CustomKpi).where(CustomKpi.tenant_id == tenant_id, CustomKpi.name == "demo_link_clicks"))).scalar_one_or_none()
        if existing is None:
            manager = await _pick_manager(session, tenant_id)
            session.add(CustomKpi(
                tenant_id=tenant_id, campaign_id=campaign_id,
                name="demo_link_clicks",
                formula={
                    "event_type": "click",
                    "filters": [{"path": "payload.utm_content", "op": "eq", "value": "demo"}],
                    "window_days": 14,
                },
                created_by=manager.id,
            ))
    step("Custom KPI 'demo_link_clicks' added (window 14d)")

    # --- 7. Generate the report ----------------------------------------
    heading("7. End-of-campaign report")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        report = await generate_report(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(UTC), generated_by="system",
        )
    step("Report v1 written", {
        "version": report.version,
        "sections": list(report.data.keys()),
    })

    # Pull the rolled-up KPI snapshot to print final headline numbers.
    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        snap = await compute_campaign_kpis(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(UTC),
        )
    kpi_dict = snap.kpis.as_dict()

    heading("Done")
    step("Headline KPIs", {
        "impressions": kpi_dict["impressions"],
        "opens": kpi_dict["opens"],
        "clicks": kpi_dict["clicks"],
        "conversions": kpi_dict["conversions"],
        "spend": f"${kpi_dict['spend']}",
        "open_rate": kpi_dict["derived"]["open_rate"],
        "ctr": kpi_dict["derived"]["ctr"],
    })
    step("URLs", {
        "campaign_detail": f"http://localhost:8001/ui/campaigns/{campaign_id}",
        "end_of_campaign_report": f"http://localhost:8001/ui/campaigns/{campaign_id}/report",
        "report_json_api": f"http://localhost:8001/api/campaigns/{campaign_id}/reports/latest",
        "report_csv_export": f"http://localhost:8001/api/campaigns/{campaign_id}/reports/latest.csv",
    })
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

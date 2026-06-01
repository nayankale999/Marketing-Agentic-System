"""End-to-end MAS demo against a REAL Anthropic API key.

Same shape as `scripts/full_demo.py` but exercises the Strategist and
Content Creator with live LLM calls instead of pre-seeded copy.
Dispatch is respx-mocked so no real emails / posts ship.

Run:    .venv/bin/python -m scripts.live_anthropic_demo
Prereq: ANTHROPIC_API_KEY set in env or .env.local. Docker daemon up.

What you'll see:
  * Real strategy JSON produced by claude-sonnet-4-6 against the brief
  * Real email + LinkedIn copy generated per touchpoint
  * Token usage printed per call + a grand total at the end
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import respx
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer


DASH = "─" * 78
DEMO_TENANT_NAME = "Acme Robotics (live LLM demo)"


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------


def heading(t: str) -> None:
    print(f"\n{DASH}\n  {t}\n{DASH}")


def step(label: str, body: Any = None) -> None:
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


@dataclass
class TokenLedger:
    """Anthropic billing rates as of 2026-05 ($USD per million tokens).
    Updated when the model changes; not authoritative — verify on the
    Anthropic pricing page for production estimates."""

    rates: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
            "claude-haiku-4-5": {"input": 0.25, "output": 1.25},
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, label: str, model: str, input_tokens: int, output_tokens: int) -> None:
        rate = self.rates.get(model, {"input": 0.0, "output": 0.0})
        cost = (input_tokens / 1_000_000) * rate["input"] + (
            output_tokens / 1_000_000
        ) * rate["output"]
        self.calls.append(
            {
                "label": label,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            }
        )

    def summary(self) -> dict[str, Any]:
        total_in = sum(c["input_tokens"] for c in self.calls)
        total_out = sum(c["output_tokens"] for c in self.calls)
        total_cost = sum(c["cost_usd"] for c in self.calls)
        return {
            "calls": len(self.calls),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cost_usd": round(total_cost, 4),
        }


ledger = TokenLedger()


def wrap_anthropic_client(client, label_for_call):
    """Wrap an AsyncAnthropic.messages.create call to record token usage."""
    original = client.messages.create

    async def wrapped(**kwargs):
        msg = await original(**kwargs)
        usage = getattr(msg, "usage", None)
        if usage is not None:
            ledger.record(
                label=label_for_call(),
                model=kwargs.get("model") or "unknown",
                input_tokens=int(getattr(usage, "input_tokens", 0)),
                output_tokens=int(getattr(usage, "output_tokens", 0)),
            )
        return msg

    client.messages.create = wrapped


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def run() -> None:
    # Settings reads .env.local; env vars override. We have to load before
    # checking because the script may be invoked without `export`-ing.
    from app.settings.config import get_settings

    if not get_settings().anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set in env or .env.local.")
        print("Add it to .env.local and re-run.")
        sys.exit(2)
    # Surface it as an env var so child code paths that read env directly
    # (anthropic SDK fallback, observability) see it too.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = get_settings().anthropic_api_key

    heading("1. Boot isolated Postgres + apply 22 migrations")
    with PostgresContainer("postgres:15-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        os.environ["DATABASE_URL"] = url
        from app.settings.config import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]

        command.upgrade(Config("alembic.ini"), "head")
        step("Postgres ready", {"url": url[: url.find("@") + 1] + "<host>:<port>/test"})

        await _walk_through_pipeline()


async def _walk_through_pipeline() -> None:
    from anthropic import AsyncAnthropic
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ab_testing.assignment import assign_variant
    from app.agents.content_creator import (
        ensure_content_creator_agent,
        generate_asset,
    )
    from app.agents.strategist import (
        ensure_strategist_agent,
        propose as strategist_propose,
    )
    from app.agents._strategist_planner import StrategistPlanner
    from app.analytics.anomaly import detect_anomalies
    from app.analytics.kpi_rollup import compute_campaign_kpis
    from app.analytics.recommendations import generate_recommendations
    from app.analytics.report import generate_report
    from app.analytics.spend_reconciliation import (
        ChannelSpend,
        ingest_platform_spend,
        run_reconciliation,
    )
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
        BrandVoice,
        Campaign,
        Channel,
        ContentAsset,
        DispatchAttempt,
        StrategyProposal,
        StrategyTouchpoint,
        Tenant,
    )
    from app.db.session import SessionLocal, set_tenant_context
    from app.settings.config import get_settings
    from app.tools.copywriting import CopywritingTool
    from app.tools.seo import SeoAnalysisTool

    register_listeners()
    settings = get_settings()

    # Build one shared Anthropic client + wrap it so we can count tokens.
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    current_label = {"value": "unknown"}
    wrap_anthropic_client(client, lambda: current_label["value"])

    strategist_planner = StrategistPlanner(client=client, model=settings.strategist_model)
    copywriting_tool = CopywritingTool(client=client, model=settings.copywriting_model)
    seo_tool = SeoAnalysisTool()

    # --- 2. tenant + brand voice ----------------------------------------
    heading("2. Seed tenant + admin + brand voice")
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(name=DEMO_TENANT_NAME)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        admin = AppUser(
            tenant_id=tenant_id,
            email="alex@acme.test",
            role=UserRole.admin,
            is_active=True,
            display_name="Alex (Acme Marketing)",
        )
        session.add(admin)
        await session.flush()
        admin_id = admin.id

        email_ch = Channel(
            tenant_id=tenant_id,
            name="Email",
            platform=ChannelPlatform.email,
            is_active=True,
        )
        li_ch = Channel(
            tenant_id=tenant_id,
            name="LinkedIn",
            platform=ChannelPlatform.linkedin,
            is_active=True,
        )
        session.add_all([email_ch, li_ch])
        await session.flush()
        email_ch_id, li_ch_id = email_ch.id, li_ch.id

        voice = BrandVoice(
            tenant_id=tenant_id,
            name="default",
            is_active=True,
            tone_descriptors=[
                "Direct",
                "Technical",
                "Confident",
                "No marketing fluff",
            ],
            do_words=[
                "3x faster",
                "specifics",
                "verified",
                "production lines",
                "throughput",
            ],
            dont_words=[
                "best-in-class",
                "solutions provider",
                "leverage synergies",
                "world-class",
                "innovative",
            ],
            sample_paragraphs=[
                "We measured 3x fewer line defects across 12 production lines.",
                "Manufacturing CTOs at mid-market plants have heard every "
                "vendor pitch — they respect specifics.",
            ],
            reading_grade_target=Decimal("9.0"),
        )
        session.add(voice)

    step("Tenant + admin + channels + brand voice", {
        "tenant_id": str(tenant_id),
        "channels": ["Email", "LinkedIn"],
        "brand_voice": "default — Direct, technical, confident",
    })

    # --- 3. campaign brief ----------------------------------------------
    heading("3. Marketer drafts campaign brief")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        campaign = Campaign(
            tenant_id=tenant_id,
            owner_id=admin_id,
            name="Q3 Vision-System Launch — Manufacturing CTOs",
            campaign_type=CampaignType.product_launch,
            objective=(
                "Drive 100 MQLs from manufacturing CTOs in 6 weeks; demo "
                "completion is the primary conversion event."
            ),
            brief=(
                "Acme Robotics is launching a new computer-vision quality-control "
                "SKU aimed at mid-market manufacturing plants. The system spots "
                "production-line defects 3× faster than the incumbent (verified "
                "across 12 customer deployments). We're targeting CTOs and VPs of "
                "Engineering at plants with 100-2000 employees in the US and CA, "
                "primarily in industrial automation + robotics + heavy "
                "manufacturing verticals. Email is the primary channel; LinkedIn "
                "is for awareness + retargeting. Avoid hype language — this "
                "audience switches off the moment they hear 'solutions provider'. "
                "Lean on specifics (3× faster, 28% throughput uplift, 12 lines "
                "deployed). Budget $25,000 over 6 weeks."
            ),
            budget_total=Decimal("25000.00"),
            currency="USD",
            start_date=date.today() - timedelta(days=2),
            end_date=date.today() + timedelta(days=40),
            kpi_targets={
                "primary": {"metric": "conversion", "target": 100},
                "secondary": [{"metric": "click", "target": 2000}],
            },
            status=CampaignStatus.audience_built,
        )
        session.add(campaign)
        await session.flush()
        campaign_id = campaign.id

        # Audience (synthetic — same as full_demo)
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
            refreshed_at=datetime.now(UTC),
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
                    fetched_at=datetime.now(UTC),
                )
            )
    step("Campaign + 20-recipient audience created", {
        "campaign_id": str(campaign_id),
        "audience": "Manufacturing CTOs — North America (n=20)",
    })

    # --- 4. REAL Strategist call ----------------------------------------
    heading("4. Strategist agent → LIVE Anthropic call")
    current_label["value"] = "strategist.propose"
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        await ensure_strategist_agent(session, tenant_id)
        summary = await strategist_propose(
            session,
            campaign_id=campaign_id,
            planner=strategist_planner,
            triggered_by_user_id=admin_id,
        )

    async with SessionLocal() as session:
        proposal = (
            await session.execute(
                select(StrategyProposal)
                .where(StrategyProposal.campaign_id == campaign_id)
                .order_by(StrategyProposal.version.desc())
                .limit(1)
            )
        ).scalar_one()
        proposal_payload = proposal.payload
        proposal_id = proposal.id

    step("Strategy proposal produced by claude", proposal_payload)

    # --- 5. Accept the proposal + lay down touchpoints ------------------
    heading("5. Marketer accepts proposal; touchpoints + asset rows created")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        prop = await session.get(StrategyProposal, proposal_id)
        prop.is_accepted = True
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.strategy_set

        # Create 3 touchpoints + a content_asset per touchpoint.
        # In production, the orchestrator + content_creator queue task
        # creates these; here we lay them down inline.
        touchpoint_dates = [
            date.today() - timedelta(days=8),
            date.today() - timedelta(days=4),
            date.today() + timedelta(days=2),
        ]
        touchpoint_ids: list[uuid.UUID] = []
        for i, when in enumerate(touchpoint_dates):
            platform = "linkedin" if i == 2 else "email"
            tp = StrategyTouchpoint(
                tenant_id=tenant_id,
                proposal_id=prop.id,
                channel_platform=platform,
                audience_id=audience_id,
                scheduled_at=datetime.combine(when, time(9, 0), UTC),
                position=i + 1,
            )
            session.add(tp)
            await session.flush()
            touchpoint_ids.append(tp.id)
        await session.flush()

        # Drafted-status asset rows that the Content Creator will fill in.
        # We pre-create one per touchpoint with channel + touchpoint metadata.
        asset_ids: list[uuid.UUID] = []
        for i, (when, tp_id) in enumerate(zip(touchpoint_dates, touchpoint_ids)):
            platform = "linkedin" if i == 2 else "email"
            asset_type = (
                AssetType.social_post if platform == "linkedin" else AssetType.email
            )
            channel_id = li_ch_id if platform == "linkedin" else email_ch_id
            asset = ContentAsset(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                channel_id=channel_id,
                asset_type=asset_type,
                status=AssetStatus.drafted,  # generate_asset transitions drafted→drafted
                title="",
                content="",
                extra_metadata={
                    "channel_platform": platform,
                    "touchpoint_id": str(tp_id),
                },
                scheduled_at=datetime.combine(when, time(9, 0), UTC),
                is_required=True,
            )
            session.add(asset)
            await session.flush()
            asset_ids.append(asset.id)

    step("Proposal accepted; 3 touchpoints + 3 draft asset rows", {
        "touchpoints": len(touchpoint_ids),
        "assets_to_generate": len(asset_ids),
    })

    # --- 6. REAL Content Creator calls ----------------------------------
    heading("6. Content Creator → LIVE Anthropic call per asset")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        await ensure_content_creator_agent(session, tenant_id)
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        for idx, aid in enumerate(asset_ids):
            current_label["value"] = f"copywriting.generate(asset {idx + 1})"
            try:
                result = await generate_asset(
                    session,
                    asset_id=aid,
                    copywriting_tool=copywriting_tool,
                    seo_tool=seo_tool,
                )
            except Exception as exc:
                step(f"Asset {idx + 1} failed", str(exc))
                continue
            asset = await session.get(ContentAsset, aid)
            fields = (asset.extra_metadata or {}).get("fields") or {}
            step(
                f"Asset {idx + 1} ({asset.asset_type.value}, {(asset.extra_metadata or {}).get('channel_platform')})",
                {
                    "status": asset.status.value,
                    "subject": fields.get("subject") or asset.title,
                    "preheader": fields.get("preheader"),
                    "body_preview": (asset.content or "")[:200],
                    "seo_score": result.get("seo_score") if isinstance(result, dict) else None,
                },
            )

    # --- 7. Approve everything (simulate manager approval) --------------
    heading("7. Manager approves all 3 assets")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        for aid in asset_ids:
            asset = await session.get(ContentAsset, aid)
            if asset.status in (AssetStatus.drafted, AssetStatus.pending_approval):
                asset.status = AssetStatus.approved
        await session.flush()
    step("All approved.")

    # --- 8. Mocked dispatch + analytics ---------------------------------
    heading("8. Dispatch via respx-mocked SendGrid (no real sends)")
    SG_API = "https://api.sendgrid.com/v3/mail/send"
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.post(SG_API).mock(
            return_value=httpx.Response(202, headers={"X-Message-Id": "msg-demo"})
        )
        async with SessionLocal() as session, session.begin():
            await set_tenant_context(session, tenant_id)
            now = datetime.now(UTC)
            # Pick the email asset for "dispatch" — write attempts + events.
            email_asset_id = asset_ids[0]
            for i in range(20):
                ext = f"mfg-cto-{i:03d}"
                email = f"cto{i:03d}@plant-{i % 5}.demo"
                sg_msg = f"sg-{ext}"
                session.add(
                    DispatchAttempt(
                        tenant_id=tenant_id,
                        content_asset_id=email_asset_id,
                        audience_external_id=ext,
                        recipient_identifier=email,
                        idempotency_key=f"k-{ext}",
                        provider="sendgrid",
                        provider_message_id=sg_msg,
                        status="sent",
                        sent_at=now - timedelta(days=5),
                    )
                )
                # Engagement: 1 open + occasional click/conversion.
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                    event_type=EventKind.open,
                    payload={"sg_message_id": sg_msg},
                    provider_event_id=f"o-{ext}", event_at=now - timedelta(days=5, hours=-i),
                ))
                if i % 2 == 0:
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                        event_type=EventKind.click,
                        payload={"sg_message_id": sg_msg, "utm_content": "demo"},
                        provider_event_id=f"c-{ext}", event_at=now - timedelta(days=5, hours=-i),
                    ))
                if i % 3 == 0:
                    session.add(AnalyticEvent(
                        tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                        event_type=EventKind.conversion,
                        payload={"sg_message_id": sg_msg},
                        provider_event_id=f"v-{ext}", event_at=now - timedelta(days=4, hours=-i),
                    ))
            # Per-channel spend.
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=email_ch_id,
                event_type=EventKind.spend, metric_value=Decimal("4200.00"), payload={},
                event_at=now - timedelta(days=1),
            ))
            session.add(AnalyticEvent(
                tenant_id=tenant_id, campaign_id=campaign_id, channel_id=li_ch_id,
                event_type=EventKind.spend, metric_value=Decimal("6800.00"), payload={},
                event_at=now - timedelta(days=1),
            ))
            # LinkedIn engagement — weaker than email.
            for j in range(15):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=li_ch_id,
                    event_type=EventKind.click, payload={},
                    provider_event_id=f"li-c-{uuid.uuid4().hex[:8]}",
                    event_at=now - timedelta(days=3),
                ))
            for _ in range(2):
                session.add(AnalyticEvent(
                    tenant_id=tenant_id, campaign_id=campaign_id, channel_id=li_ch_id,
                    event_type=EventKind.conversion, payload={},
                    provider_event_id=f"li-v-{uuid.uuid4().hex[:8]}",
                    event_at=now - timedelta(days=3),
                ))

    step("Dispatch + analytics seeded", {
        "dispatch_attempts": 20,
        "events": "opens + clicks + conversions + spend (no real sends)",
    })

    # --- 9. KPI rollup --------------------------------------------------
    heading("9. Real-time KPI snapshot")
    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        snap = await compute_campaign_kpis(
            session, tenant_id=tenant_id, campaign_id=campaign_id, now=datetime.now(UTC)
        )
    kpi_dict = snap.kpis.as_dict()
    step("KPIs", {
        "opens": kpi_dict["opens"],
        "clicks": kpi_dict["clicks"],
        "conversions": kpi_dict["conversions"],
        "spend": kpi_dict["spend"],
        "open_rate": kpi_dict["derived"]["open_rate"],
        "ctr": kpi_dict["derived"]["ctr"],
    })

    # --- 10. Anomaly + recommendation -----------------------------------
    heading("10. Analytics & Optimisation pass")
    # Seed a baseline + spike so the detector has something to flag.
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        baseline_start = datetime.now(UTC) - timedelta(days=13)
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
                event_at=datetime.now(UTC) - timedelta(hours=4),
            ))

    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        anomalies = await detect_anomalies(
            session, tenant_id=tenant_id, campaign_id=campaign_id, now=datetime.now(UTC)
        )
        recs = await generate_recommendations(
            session, tenant_id=tenant_id, campaign_id=campaign_id, now=datetime.now(UTC)
        )
        await ingest_platform_spend(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            records=[
                ChannelSpend(channel_id=email_ch_id, amount=Decimal("4200.00")),
                ChannelSpend(channel_id=li_ch_id, amount=Decimal("6800.00")),
            ],
        )
        await run_reconciliation(
            session, tenant_id=tenant_id,
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            invoices={campaign_id: Decimal("11330.00")},
        )

    step("Anomalies", [
        {"metric": a.metric, "severity": a.severity, "sigma": str(a.sigma)}
        for a in anomalies
    ] or "(none)")
    step("Recommendations", [
        {"kind": r.kind, "predicted_uplift": str(r.predicted_uplift),
         "rationale": (r.rationale or "")[:140] + "..." if r.rationale and len(r.rationale) > 140 else r.rationale}
        for r in recs
    ] or "(none)")

    # --- 11. End-of-campaign report -------------------------------------
    heading("11. End-of-campaign report")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        report = await generate_report(
            session, tenant_id=tenant_id, campaign_id=campaign_id,
            now=datetime.now(UTC), generated_by="system",
        )
    step("Report v1 written", {
        "version": report.version,
        "is_latest": report.is_latest,
        "sections": list(report.data.keys()),
    })

    # --- 12. Token + cost summary ---------------------------------------
    heading("12. Anthropic token usage")
    for c in ledger.calls:
        print(f"  • {c['label']:<45}  in={c['input_tokens']:>5}  out={c['output_tokens']:>5}  ${c['cost_usd']:.4f}  ({c['model']})")
    s = ledger.summary()
    print(
        f"\n  TOTAL:  {s['calls']} calls  "
        f"{s['total_input_tokens']} input + {s['total_output_tokens']} output tokens  "
        f"→ ${s['total_cost_usd']:.4f}"
    )
    print(
        "\n  (Costs use 2026-05 list prices. Real bill from Anthropic\n"
        "   is authoritative.)\n"
    )


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

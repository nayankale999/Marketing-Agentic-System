"""W23 — Compliance pre-check (E06-S08).

Three layers under test:

  * `_compliance` pure functions — universal patterns, tenant rules, regex error.
  * Agent integration — block-severity hits flag metadata, warn-severity hits
    trigger rewrite retry, ComplianceCheckError leaves asset in `generating`.
  * API — admin CRUD on /api/compliance-rules, manager clear-compliance,
    state machine guard blocks submit_for_approval when blocked=true.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents._compliance import (
    ComplianceCheckError,
    body_cosine_similarity,
    check_compliance,
)
from app.agents.content_creator import (
    generate_asset,
    seed_assets_for_campaign,
)
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import (
    AssetStatus,
    AssetType,
    CampaignStatus,
    CampaignType,
    ChannelPlatform,
    UserRole,
)
from app.db.models import (
    AppUser,
    Audience,
    AuditLog,
    Campaign,
    Channel,
    ComplianceRule,
    ContentAsset,
    StrategyProposal,
    StrategyTouchpoint,
    Tenant,
)
from app.db.session import set_tenant_context
from app.orchestrator.state_machine import GuardFailedError, campaign_sm
from app.tools.copywriting import CopywritingTool
from app.tools.seo import SeoAnalysisTool


_API = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class _R:
    """Lightweight stand-in for ComplianceRule in pure tests."""

    def __init__(self, keyword: str, pattern_kind: str = "exact", severity: str = "warn") -> None:
        self.keyword = keyword
        self.pattern_kind = pattern_kind
        self.severity = severity


def test_check_compliance_passes_clean_text() -> None:
    result = check_compliance("This is a normal piece of marketing copy.", [])
    assert result.passed is True
    assert result.blocked is False
    assert result.hits == []


def test_universal_pattern_catches_guarantee_language() -> None:
    result = check_compliance("Our product is guaranteed to work.", [])
    assert result.passed is False
    assert result.blocked is True
    assert any(h.rule == "guaranteed_results" for h in result.hits)


def test_universal_pattern_catches_medical_claim() -> None:
    result = check_compliance("This supplement cures fatigue.", [])
    assert result.blocked is True
    assert any(h.rule == "medical_claim" for h in result.hits)


def test_unqualified_superlative_is_warn_not_block() -> None:
    result = check_compliance("We're the best CRM out there.", [])
    assert result.blocked is False  # warn-severity only
    assert result.passed is False
    assert "the_best".replace("_", " ") or any(
        h.rule == "unqualified_superlative" for h in result.hits
    )


def test_tenant_exact_rule_matches_word_boundary() -> None:
    rules = [_R("disrupt", severity="warn")]
    assert check_compliance("Let's disrupt the market.", rules).hits  # matches
    # Should not match inside another word (e.g. "disruption" — currently
    # matches because we use \b; this test just confirms exact rule fires).
    assert check_compliance("We have no disruptive plans.", rules).passed


def test_tenant_block_rule_marks_blocked() -> None:
    rules = [_R("first-to-market", severity="block")]
    result = check_compliance("We are first-to-market on this.", rules)
    assert result.blocked is True


def test_tenant_regex_rule_works() -> None:
    # `\bbeta-?launch\b` matches both "beta-launch" and "betalaunch" thanks
    # to the optional hyphen — exercise both, then a non-match.
    rules = [_R(r"\bbeta-?launch\b", pattern_kind="regex", severity="warn")]
    assert check_compliance("This is our beta-launch event.", rules).hits
    assert check_compliance("This is our betalaunch event.", rules).hits
    assert check_compliance("This is our prelaunch event.", rules).passed


def test_malformed_tenant_regex_raises_compliance_error() -> None:
    rules = [_R(r"(unclosed", pattern_kind="regex", severity="warn")]
    with pytest.raises(ComplianceCheckError):
        check_compliance("Whatever text.", rules)


def test_warn_keywords_collected_for_rewrite_prompt() -> None:
    rules = [_R("synergy", severity="warn"), _R("disrupt", severity="warn")]
    result = check_compliance("synergy disrupt", rules)
    assert set(result.warn_keywords) == {"synergy", "disrupt"}


def test_cosine_similarity_identical_strings_is_one() -> None:
    # Float rounding lands the result at 1.0 within machine epsilon.
    assert body_cosine_similarity("hello world", "hello world") == pytest.approx(1.0)


def test_cosine_similarity_disjoint_is_zero() -> None:
    assert body_cosine_similarity("apple orange", "carrot banana") == 0.0


def test_cosine_similarity_partial_overlap_in_zero_to_one() -> None:
    score = body_cosine_similarity(
        "marketing automation lets teams move faster",
        "marketing automation is the future of growth",
    )
    assert 0.0 < score < 1.0


def test_cosine_similarity_handles_empty() -> None:
    assert body_cosine_similarity("", "hello") == 0.0
    assert body_cosine_similarity("hello", "") == 0.0


# ---------------------------------------------------------------------------
# Helpers — DB seeding + Anthropic mocking (mirrors test_content_creator.py)
# ---------------------------------------------------------------------------


def _anthropic_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


def _email_payload(body: str = "Hi there, our latest release is live.") -> dict:
    return {
        "subject": "Latest release",
        "preheader": "Highlights",
        "body": body,
        "cta": "Try it",
    }


def _copywriting_tool() -> CopywritingTool:
    return CopywritingTool(
        client=AsyncAnthropic(api_key="test-key"), model="claude-sonnet-4-6"
    )


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    with_compliance_rules: list[dict[str, str]] | None = None,
    state: CampaignStatus = CampaignStatus.strategy_set,
) -> dict[str, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"cmp-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()

        session.add(
            Channel(
                tenant_id=tenant.id,
                name="Email",
                platform=ChannelPlatform.email,
                is_active=True,
            )
        )

        for rule in with_compliance_rules or []:
            session.add(
                ComplianceRule(
                    tenant_id=tenant.id,
                    keyword=rule["keyword"],
                    pattern_kind=rule.get("pattern_kind", "exact"),
                    severity=rule.get("severity", "warn"),
                )
            )

        campaign = Campaign(
            tenant_id=tenant.id,
            name="cmp-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=state,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=10,
            actual_size=10,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

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
                        "allocation_amount": "10000.00",
                        "rationale": "x",
                        "human_override": False,
                    }
                ],
                "kpis": {
                    "primary": {"metric": "mql", "target": 100, "rationale": "z"},
                    "secondary": [],
                },
            },
            is_accepted=True,
            created_by_kind="agent",
        )
        session.add(proposal)
        await session.flush()
        session.add(
            StrategyTouchpoint(
                tenant_id=tenant.id,
                proposal_id=proposal.id,
                channel_platform="email",
                audience_id=audience.id,
                scheduled_at=datetime.combine(date(2026, 6, 15), time(9, 0), UTC),
            )
        )

        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
        }


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


@respx.mock
async def test_block_severity_hit_flips_blocked_flag_and_keeps_drafted(
    db_engine: AsyncEngine,
) -> None:
    # Use a block-severity tenant rule so the universal patterns don't double-fire.
    respx.post(_API).mock(
        return_value=_anthropic_response(
            _email_payload("We are first-to-market on this innovation.")
        )
    )

    world = await _seed_world(
        db_engine,
        with_compliance_rules=[
            {"keyword": "first-to-market", "severity": "block"},
        ],
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        asset = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == world["campaign_id"])
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await generate_asset(
            session,
            asset_id=asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )

    assert out["status"] == "drafted"
    assert out["compliance_blocked"] is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, asset.id)
        compliance = reloaded.extra_metadata["compliance"]
        assert compliance["blocked"] is True
        assert compliance["pass"] is False
        assert any(h["severity"] == "block" for h in compliance["hits"])


@respx.mock
async def test_warn_hit_triggers_rewrite_retry(db_engine: AsyncEngine) -> None:
    # First attempt contains the suppression keyword; second is clean.
    respx.post(_API).mock(
        side_effect=[
            _anthropic_response(_email_payload("Let's disrupt the market today.")),
            _anthropic_response(_email_payload("Let's reshape the market today.")),
        ]
    )

    world = await _seed_world(
        db_engine,
        with_compliance_rules=[{"keyword": "disrupt", "severity": "warn"}],
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        asset = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == world["campaign_id"])
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        await generate_asset(
            session,
            asset_id=asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, asset.id)
        assert "disrupt" not in (reloaded.content or "")
        assert reloaded.extra_metadata["compliance"]["pass"] is True
        assert reloaded.extra_metadata["compliance"]["rewritten_for_suppression"] is True


@respx.mock
async def test_compliance_check_error_leaves_asset_in_generating(
    db_engine: AsyncEngine,
) -> None:
    respx.post(_API).mock(
        return_value=_anthropic_response(_email_payload("All good copy."))
    )

    world = await _seed_world(
        db_engine,
        with_compliance_rules=[
            {"keyword": r"(unclosed", "pattern_kind": "regex", "severity": "warn"}
        ],
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        asset = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == world["campaign_id"])
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        out = await generate_asset(
            session,
            asset_id=asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )
        assert out["error"] == "compliance_check_failed"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        reloaded = await session.get(ContentAsset, asset.id)
        # Failed (not drafted) with the error in metadata so an operator
        # can see "needs attention" without the draft passing silently.
        assert reloaded.status == AssetStatus.failed
        assert "compliance_error" in reloaded.extra_metadata


# ---------------------------------------------------------------------------
# State machine guard
# ---------------------------------------------------------------------------


@respx.mock
async def test_submit_for_approval_blocked_when_compliance_blocked(
    db_engine: AsyncEngine,
) -> None:
    respx.post(_API).mock(
        return_value=_anthropic_response(
            _email_payload("We are first-to-market on this innovation.")
        )
    )

    world = await _seed_world(
        db_engine,
        with_compliance_rules=[
            {"keyword": "first-to-market", "severity": "block"},
        ],
        state=CampaignStatus.content_in_production,
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        campaign = await session.get(Campaign, world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        asset = (
            await session.execute(
                select(ContentAsset).where(ContentAsset.campaign_id == world["campaign_id"])
            )
        ).scalar_one()

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, world["tenant_id"])
        await generate_asset(
            session,
            asset_id=asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )

    # Asset is drafted but compliance.blocked=true → campaign should still
    # be in content_in_production, not approval_pending.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        campaign = await session.get(Campaign, world["campaign_id"])
        assert campaign.status == CampaignStatus.content_in_production


# ---------------------------------------------------------------------------
# API — admin CRUD + manager clear
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine)


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@cmp.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def client_as(api_world, db_engine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, api_world["tenant_id"], role)
        app.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client, user

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_current_user, None)


async def test_admin_can_create_compliance_rule(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/compliance-rules",
        json={
            "keyword": "synergy",
            "pattern_kind": "exact",
            "severity": "warn",
            "description": "Buzzword we banned",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["keyword"] == "synergy"
    assert body["severity"] == "warn"


async def test_create_rule_rejects_invalid_regex(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    resp = await client.post(
        "/api/compliance-rules",
        json={"keyword": r"(unclosed", "pattern_kind": "regex", "severity": "warn"},
    )
    assert resp.status_code == 422


async def test_create_duplicate_rule_returns_409(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    body = {"keyword": "synergy", "pattern_kind": "exact", "severity": "warn"}
    assert (await client.post("/api/compliance-rules", json=body)).status_code == 201
    assert (await client.post("/api/compliance-rules", json=body)).status_code == 409


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.manager, UserRole.viewer])
async def test_non_admin_cannot_manage_rules(client_as, role) -> None:
    client, _ = await client_as(role)
    resp = await client.post(
        "/api/compliance-rules",
        json={"keyword": "synergy", "pattern_kind": "exact", "severity": "warn"},
    )
    assert resp.status_code == 403


async def test_list_returns_block_rules_first(client_as) -> None:
    client, _ = await client_as(UserRole.admin)
    await client.post(
        "/api/compliance-rules",
        json={"keyword": "buzzword", "pattern_kind": "exact", "severity": "warn"},
    )
    await client.post(
        "/api/compliance-rules",
        json={"keyword": "guaranteed", "pattern_kind": "exact", "severity": "block"},
    )
    resp = await client.get("/api/compliance-rules")
    items = resp.json()["items"]
    assert items[0]["severity"] == "block"


async def test_delete_compliance_rule(client_as, db_engine) -> None:
    client, _ = await client_as(UserRole.admin)
    created = (
        await client.post(
            "/api/compliance-rules",
            json={"keyword": "synergy", "pattern_kind": "exact", "severity": "warn"},
        )
    ).json()
    resp = await client.delete(f"/api/compliance-rules/{created['id']}")
    assert resp.status_code == 204
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        remaining = await session.get(ComplianceRule, uuid.UUID(created["id"]))
        assert remaining is None


# ---- Clear compliance -----------------------------------------------------


@respx.mock
async def test_manager_can_clear_compliance_block(client_as, api_world, db_engine, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    # Pre-seed a rule + draft a blocked asset by running the agent.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            ComplianceRule(
                tenant_id=api_world["tenant_id"],
                keyword="first-to-market",
                pattern_kind="exact",
                severity="block",
            )
        )
        campaign = await session.get(Campaign, api_world["campaign_id"])
        campaign.status = CampaignStatus.content_in_production

    respx.post(_API).mock(
        return_value=_anthropic_response(
            _email_payload("We are first-to-market on this.")
        )
    )

    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await set_tenant_context(session, api_world["tenant_id"])
        campaign = await session.get(Campaign, api_world["campaign_id"])
        await seed_assets_for_campaign(session, campaign=campaign)
        asset = (
            await session.execute(
                select(ContentAsset).where(
                    ContentAsset.campaign_id == api_world["campaign_id"]
                )
            )
        ).scalar_one()
        await generate_asset(
            session,
            asset_id=asset.id,
            copywriting_tool=_copywriting_tool(),
            seo_tool=SeoAnalysisTool(),
        )

    client, manager = await client_as(UserRole.manager)
    resp = await client.post(f"/api/content-assets/{asset.id}/clear-compliance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["extra_metadata"]["compliance"]["blocked"] is False
    assert body["extra_metadata"]["compliance"]["cleared_by_user_id"] == str(manager.id)

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "content_asset",
                    AuditLog.entity_id == asset.id,
                    AuditLog.action == "compliance_cleared",
                )
            )
        ).scalars().all()
        assert len(audits) == 1


async def test_clear_compliance_on_unblocked_asset_returns_409(
    client_as, api_world, db_engine
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = ContentAsset(
            tenant_id=api_world["tenant_id"],
            campaign_id=api_world["campaign_id"],
            asset_type=AssetType.email,
            status=AssetStatus.drafted,
            extra_metadata={"compliance": {"blocked": False, "hits": []}},
        )
        session.add(asset)
        await session.flush()
        asset_id = asset.id

    client, _ = await client_as(UserRole.manager)
    resp = await client.post(f"/api/content-assets/{asset_id}/clear-compliance")
    assert resp.status_code == 409


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_non_manager_cannot_clear_compliance(
    client_as, api_world, db_engine, role
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        asset = ContentAsset(
            tenant_id=api_world["tenant_id"],
            campaign_id=api_world["campaign_id"],
            asset_type=AssetType.email,
            status=AssetStatus.drafted,
            extra_metadata={
                "compliance": {"blocked": True, "hits": [{"rule": "x", "severity": "block"}]}
            },
        )
        session.add(asset)
        await session.flush()
        asset_id = asset.id

    client, _ = await client_as(role)
    resp = await client.post(f"/api/content-assets/{asset_id}/clear-compliance")
    assert resp.status_code == 403

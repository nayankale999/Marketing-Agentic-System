"""W32 — Approval queue + review UI + decision form handlers (E13-S03).

Markup is shaped to be robust to CSS tweaks. The decision-form POST
handlers go through the same persistence path as the API; tests verify
the HTML response + that the dispatch_attempt / ApprovalDecisionLog rows
landed correctly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
    ApprovalDecisionLog,
    Audience,
    Campaign,
    Channel,
    ContentAsset,
    Tenant,
)


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    asset_status: AssetStatus = AssetStatus.pending_approval,
    compliance_blocked: bool = False,
) -> dict[str, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"ar-{uuid.uuid4().hex[:6]}")
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
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"o-{uuid.uuid4().hex[:6]}@ar.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name="UI review campaign",
            campaign_type=CampaignType.product_launch,
            objective="Test objective",
            brief="Brief text",
            budget_total=Decimal("1000.00"),
            currency="USD",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
            status=CampaignStatus.content_in_production,
        )
        session.add(campaign)
        await session.flush()
        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="UI audience",
            segment_criteria={},
            estimated_size=10,
            actual_size=10,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

        metadata: dict = {
            "channel_platform": "email",
            "fields": {
                "subject": "Welcome to the SMB tier",
                "preheader": "An early look",
                "cta": "Reply yes",
            },
            "brand_check": {"pass": True, "failing_words": []},
        }
        if compliance_blocked:
            metadata["compliance"] = {
                "blocked": True,
                "hits": [
                    {"rule": "x", "severity": "block", "snippet": "guaranteed"}
                ],
            }

        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            asset_type=AssetType.email,
            status=asset_status,
            title="Welcome to the SMB tier",
            content="Hi {{first_name}}, here's an early look at SMB tier.",
            extra_metadata=metadata,
            is_required=True,
            updated_at=datetime.now(UTC) - timedelta(hours=2),
        )
        session.add(asset)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "campaign_id": campaign.id,
            "asset_id": asset.id,
        }


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@ar.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(db_engine)


@pytest.fixture
async def client_as(world, db_engine: AsyncEngine) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(role: UserRole) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, world["tenant_id"], role)
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


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


async def test_queue_lists_pending_assets(client_as, world) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.get("/ui/approvals/queue")
    assert resp.status_code == 200
    body = resp.text
    assert "Approvals" in body
    assert "Pending your review" in body
    assert "Welcome to the SMB tier" in body
    assert f"/ui/approvals/{world['asset_id']}" in body


async def test_queue_shows_empty_state_when_nothing_pending(
    db_engine: AsyncEngine, override_api_db
) -> None:
    """Fresh tenant with no pending assets renders an empty state."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"empty-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    user = await _make_user(db_engine, tenant_id, UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ui/approvals/queue")
            assert resp.status_code == 200
            # New template renders three sections; each shows its own
            # empty-state when nothing matches.
            assert "Nothing has been formally submitted" in resp.text
            assert "No drafts in the queue" in resp.text
            assert "No decisions recorded yet" in resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_queue_requires_manager_role(client_as, role) -> None:
    client, _ = await client_as(role)
    resp = await client.get("/ui/approvals/queue")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------


async def test_review_page_renders_full_context(client_as, world) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.get(f"/ui/approvals/{world['asset_id']}")
    assert resp.status_code == 200
    body = resp.text

    # Header
    assert "Welcome to the SMB tier" in body
    assert "pending_approval" in body

    # Brief surfaces objective + brief
    assert "Test objective" in body
    assert "Brief text" in body

    # Audience block
    assert "UI audience" in body

    # Brand check pass badge
    assert "pass" in body  # the badge text

    # Preview frame contains the rendered body with default merge values
    assert "Alex" in body  # default first_name

    # All three decision actions are present
    assert "Approve</button>" in body or 'type="submit"' in body
    assert "Approve with edits" in body
    assert "Reject" in body


async def test_review_page_shows_compliance_banner_when_blocked(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, compliance_blocked=True)
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/ui/approvals/{world['asset_id']}")
            assert resp.status_code == 200
            assert "compliance blocked" in resp.text
            assert "clear-compliance" in resp.text  # link/instruction
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_review_page_404_for_unknown_asset(client_as) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.get(f"/ui/approvals/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_review_requires_manager_role(client_as, world, role) -> None:
    client, _ = await client_as(role)
    resp = await client.get(f"/ui/approvals/{world['asset_id']}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Decision form: approve
# ---------------------------------------------------------------------------


async def test_approve_form_writes_decision_and_returns_fragment(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, manager = await client_as(UserRole.manager)
    resp = await client.post(
        f"/ui/approvals/{world['asset_id']}/approve",
        data={},  # no edits — straight approve
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Decision recorded" in body
    assert "Approved" in body

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, world["asset_id"])
        # Auto-advance kicks in: with only one required asset, the
        # campaign drives through approval_pending → ready_to_launch
        # and the start_launch on_enter hook schedules the asset.
        assert asset.status in {AssetStatus.approved, AssetStatus.scheduled}
        decisions = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id == world["asset_id"]
                )
            )
        ).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].decision.value == "approved"
        assert decisions[0].reviewer_id == manager.id


async def test_approve_with_edits_persists_diff(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/ui/approvals/{world['asset_id']}/approve",
        data={
            "edited_content": "Hi {{first_name}}, edited copy here.",
            "edit_field_subject": "New subject line",
            "note": "Trimmed CTA copy",
        },
    )
    assert resp.status_code == 200
    assert "Approved with edits" in resp.text

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, world["asset_id"])
        assert asset.status in {AssetStatus.approved, AssetStatus.scheduled}
        assert "edited copy" in (asset.content or "")
        assert asset.extra_metadata["fields"]["subject"] == "New subject line"
        decisions = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id == world["asset_id"]
                )
            )
        ).scalars().all()
        assert decisions[0].decision.value == "approved_with_edits"
        assert decisions[0].edits["previous_content"].startswith("Hi {{first_name}}")
        assert decisions[0].edits["current_fields"]["subject"] == "New subject line"


async def test_approve_refuses_compliance_blocked(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, compliance_blocked=True)
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/ui/approvals/{world['asset_id']}/approve", data={}
            )
            assert resp.status_code == 200
            assert "compliance-blocked" in resp.text.lower()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_approve_refuses_wrong_status(
    db_engine: AsyncEngine, override_api_db
) -> None:
    world = await _seed_world(db_engine, asset_status=AssetStatus.approved)
    user = await _make_user(db_engine, world["tenant_id"], UserRole.manager)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/ui/approvals/{world['asset_id']}/approve", data={}
            )
            assert resp.status_code == 200
            assert "not eligible" in resp.text.lower()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Decision form: reject
# ---------------------------------------------------------------------------


async def test_reject_form_writes_decision_and_returns_fragment(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/ui/approvals/{world['asset_id']}/reject",
        data={"reason": "Off-voice opening", "category": "off_voice"},
    )
    assert resp.status_code == 200
    assert "Decision recorded" in resp.text
    assert "off_voice" in resp.text

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        asset = await session.get(ContentAsset, world["asset_id"])
        assert asset.status == AssetStatus.rejected
        decisions = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id == world["asset_id"]
                )
            )
        ).scalars().all()
        assert decisions[0].decision.value == "rejected"
        assert decisions[0].reason == "Off-voice opening"


async def test_reject_form_requires_reason(client_as, world) -> None:
    """FastAPI's Form(...) rejects empty strings with 422 at the validation
    layer, before reaching the handler. That's the right guardrail — the
    marketer's browser HTML5 `required` blocks submit, and a curl-bypass
    still hits the validation wall."""
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/ui/approvals/{world['asset_id']}/reject",
        data={"reason": "", "category": "other"},
    )
    assert resp.status_code == 422


async def test_reject_form_normalises_unknown_category(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.manager)
    resp = await client.post(
        f"/ui/approvals/{world['asset_id']}/reject",
        data={"reason": "Some reason", "category": "made-up"},
    )
    assert resp.status_code == 200
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        decisions = (
            await session.execute(
                select(ApprovalDecisionLog).where(
                    ApprovalDecisionLog.content_asset_id == world["asset_id"]
                )
            )
        ).scalars().all()
        # Falls back to "other" when the category isn't in the enum.
        assert decisions[0].edits["category"] == "other"


@pytest.mark.parametrize("role", [UserRole.marketer, UserRole.viewer])
async def test_decision_endpoints_require_manager(client_as, world, role) -> None:
    client, _ = await client_as(role)
    assert (
        await client.post(f"/ui/approvals/{world['asset_id']}/approve", data={})
    ).status_code == 403
    assert (
        await client.post(
            f"/ui/approvals/{world['asset_id']}/reject",
            data={"reason": "x", "category": "other"},
        )
    ).status_code == 403

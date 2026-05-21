"""W14 — segmentation.estimate + segmentation.build (E11-S04)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.audiences.segmentation import (
    FieldUnavailableError,
    OperatorUnsupportedError,
    SegmentationError,
    build,
    estimate,
    validate_criteria,
)
from app.db.enums import CampaignType, UserRole
from app.db.models import AppUser, Audience, AudienceMember, Campaign, Tenant

# ---------------------------------------------------------------------------
# validate_criteria (pure)
# ---------------------------------------------------------------------------


def test_validate_criteria_happy_path() -> None:
    parsed = validate_criteria(
        {
            "include": [
                {"field": "country", "op": "eq", "value": "US"},
                {"field": "tags", "op": "has", "value": "vip"},
            ],
            "exclude": [{"field": "tags", "op": "has", "value": "blocked"}],
        }
    )
    assert [r.field for r in parsed.include] == ["country", "tags"]
    assert parsed.exclude[0].op == "has"


def test_validate_criteria_empty_returns_empty_lists() -> None:
    parsed = validate_criteria({})
    assert parsed.include == []
    assert parsed.exclude == []


def test_validate_criteria_unknown_field_raises() -> None:
    with pytest.raises(FieldUnavailableError) as exc_info:
        validate_criteria({"include": [{"field": "salary", "op": "eq", "value": "100"}]})
    assert exc_info.value.field == "salary"
    assert exc_info.value.section == "include"


def test_validate_criteria_bad_operator_for_field_raises() -> None:
    with pytest.raises(OperatorUnsupportedError) as exc_info:
        # `tags` doesn't support `in`
        validate_criteria({"include": [{"field": "tags", "op": "in", "value": ["a"]}]})
    assert exc_info.value.op == "in"
    assert exc_info.value.field == "tags"


def test_validate_criteria_in_requires_list() -> None:
    with pytest.raises(SegmentationError):
        validate_criteria({"include": [{"field": "country", "op": "in", "value": "US"}]})


def test_validate_criteria_eq_requires_non_empty_string() -> None:
    with pytest.raises(SegmentationError):
        validate_criteria({"include": [{"field": "country", "op": "eq", "value": ""}]})


# ---------------------------------------------------------------------------
# estimate + build (against the DB)
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_tenant(db_engine: AsyncEngine) -> uuid.UUID:
    """Tenant with one campaign + one audience populated with 4 members."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"seg-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        owner = AppUser(
            tenant_id=tenant.id,
            email=f"owner-{uuid.uuid4().hex[:6]}@seg.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(owner)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=owner.id,
            name=f"camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.lead_gen,
            objective="seg demo",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
        )
        session.add(campaign)
        await session.flush()
        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seed",
            segment_criteria={"source": "csv"},
            actual_size=4,
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()
        members = [
            ("ada@example.com", {"country": "GB", "first_name": "Ada", "tags": ["vip"]}),
            ("bob@example.com", {"country": "US", "first_name": "Bob", "tags": ["vip", "early"]}),
            ("carol@example.com", {"country": "US", "first_name": "Carol", "tags": []}),
            ("dave@example.com", {"country": "DE", "first_name": "Dave", "tags": ["blocked"]}),
        ]
        for ext_id, payload in members:
            session.add(
                AudienceMember(
                    audience_id=audience.id,
                    external_id=ext_id,
                    payload={"email": ext_id, **payload},
                    source="csv",
                    fetched_at=datetime.now(UTC),
                )
            )
        return tenant.id


async def test_estimate_eq_filter(db_engine: AsyncEngine, seeded_tenant: uuid.UUID) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "country", "op": "eq", "value": "US"}]},
        )
    assert result.total_reachable == 2
    assert result.suppressed == 0
    assert result.net == 2


async def test_estimate_tags_has(db_engine: AsyncEngine, seeded_tenant: uuid.UUID) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "tags", "op": "has", "value": "vip"}]},
        )
    assert result.total_reachable == 2


async def test_estimate_in_filter(db_engine: AsyncEngine, seeded_tenant: uuid.UUID) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "country", "op": "in", "value": ["US", "DE"]}]},
        )
    assert result.total_reachable == 3


async def test_estimate_contains_case_insensitive(
    db_engine: AsyncEngine, seeded_tenant: uuid.UUID
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "first_name", "op": "contains", "value": "AD"}]},
        )
    # "Ada" matches lowercase 'ad' substring; "Dave" doesn't (no 'ad' run).
    # The point is that ILIKE compares case-insensitively.
    assert result.total_reachable == 1


async def test_estimate_combines_include_and_exclude(
    db_engine: AsyncEngine, seeded_tenant: uuid.UUID
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={
                "include": [{"field": "country", "op": "in", "value": ["US", "GB"]}],
                "exclude": [{"field": "tags", "op": "has", "value": "blocked"}],
            },
        )
    # GB Ada + US Bob + US Carol = 3
    assert result.total_reachable == 3


async def test_estimate_other_tenant_is_isolated(
    db_engine: AsyncEngine, seeded_tenant: uuid.UUID
) -> None:
    other_tenant = uuid.uuid4()  # never inserted -> 0 matches
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=other_tenant,
            criteria={"include": [{"field": "country", "op": "eq", "value": "US"}]},
        )
    assert result.total_reachable == 0


async def test_build_returns_deduped_external_ids(
    db_engine: AsyncEngine, seeded_tenant: uuid.UUID
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        ext_ids = await build(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "tags", "op": "has", "value": "vip"}]},
        )
    assert sorted(ext_ids) == ["ada@example.com", "bob@example.com"]


async def test_build_dedupes_across_audiences(
    db_engine: AsyncEngine, seeded_tenant: uuid.UUID
) -> None:
    # Add a second audience containing one of the existing contacts.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        # Reuse an existing campaign via the tenant.
        campaign_id = (
            await session.execute(
                __import__("sqlalchemy")
                .select(Campaign.id)
                .where(Campaign.tenant_id == seeded_tenant)
            )
        ).scalar_one()
        second = Audience(
            tenant_id=seeded_tenant,
            campaign_id=campaign_id,
            name="overlap",
            segment_criteria={"source": "csv"},
            actual_size=1,
            refreshed_at=datetime.now(UTC),
        )
        session.add(second)
        await session.flush()
        session.add(
            AudienceMember(
                audience_id=second.id,
                external_id="ada@example.com",  # duplicate from first audience
                payload={"email": "ada@example.com", "country": "GB", "tags": ["vip"]},
                source="csv",
                fetched_at=datetime.now(UTC),
            )
        )

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        result = await estimate(
            session,
            tenant_id=seeded_tenant,
            criteria={"include": [{"field": "country", "op": "eq", "value": "GB"}]},
        )
    assert result.total_reachable == 1  # ada counted once even though she's in 2 audiences


# ---------------------------------------------------------------------------
# /api/audiences/estimate
# ---------------------------------------------------------------------------


@pytest.fixture
async def client_marketer(
    override_api_db,
    db_engine: AsyncEngine,
    seeded_tenant: uuid.UUID,
) -> AsyncIterator:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=seeded_tenant,
            email=f"marketer-{uuid.uuid4().hex[:6]}@seg.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)

    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_current_user, None)


async def test_estimate_endpoint_happy_path(client_marketer: httpx.AsyncClient) -> None:
    resp = await client_marketer.post(
        "/api/audiences/estimate",
        json={"include": [{"field": "country", "op": "eq", "value": "US"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_reachable"] == 2
    assert body["suppressed"] == 0
    assert body["net"] == 2


async def test_estimate_endpoint_unknown_field_returns_422(
    client_marketer: httpx.AsyncClient,
) -> None:
    resp = await client_marketer.post(
        "/api/audiences/estimate",
        json={"include": [{"field": "salary", "op": "eq", "value": "100"}]},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["section"] == "include"
    assert detail["index"] == 0
    assert "salary" in detail["message"]


async def test_estimate_endpoint_requires_marketer(
    override_api_db,
    db_engine: AsyncEngine,
    seeded_tenant: uuid.UUID,
) -> None:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        viewer = AppUser(
            tenant_id=seeded_tenant,
            email=f"viewer-{uuid.uuid4().hex[:6]}@seg.test",
            role=UserRole.viewer,
            is_active=True,
        )
        session.add(viewer)
        await session.flush()
        await session.refresh(viewer)

    app.dependency_overrides[get_current_user] = lambda: viewer
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/audiences/estimate",
                json={"include": [{"field": "country", "op": "eq", "value": "US"}]},
            )
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)

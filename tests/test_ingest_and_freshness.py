"""W13 — provenance, freshness, ingest dashboard (E01-S05, E01-S06)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import CampaignType, TaskStatus, UserRole
from app.db.models import AppUser, AudienceMember, Campaign, Task, Tenant


@pytest.fixture
async def campaign_and_marketer(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, AppUser, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"w13-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            email=f"marketer-{uuid.uuid4().hex[:6]}@w13.test",
            role=UserRole.marketer,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=user.id,
            name=f"camp-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.lead_gen,
            objective="W13 demo",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=14),
        )
        session.add(campaign)
        await session.flush()
        return tenant.id, user, campaign.id


@pytest.fixture
async def client_marketer(
    override_api_db,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
) -> AsyncIterator:
    _, user, _ = campaign_and_marketer
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_current_user, None)


# -- Provenance (E01-S05) ----------------------------------------------------


async def test_csv_upload_stamps_source_and_fetched_at_on_each_member(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    csv_text = "email\na@x.test\nb@x.test\n"
    files = {"file": ("seed.csv", csv_text.encode(), "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    audience_id = uuid.UUID(resp.json()["audience_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        members = (
            (
                await session.execute(
                    select(AudienceMember).where(AudienceMember.audience_id == audience_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(members) == 2
    for m in members:
        assert m.source == "csv"
        assert m.fetched_at is not None
        assert m.fetched_at <= datetime.now(UTC)


# -- Ingest job written + visible (E01-S06) ---------------------------------


async def test_csv_upload_writes_ingest_job_task_row(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    csv_text = "email\nok@x.test\n,\nbad@@\nok@x.test\n"
    files = {"file": ("partial.csv", csv_text.encode(), "text/csv")}
    upload = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences/upload", files=files
    )
    audience_id = uuid.UUID(upload.json()["audience_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = (
            await session.execute(
                select(Task).where(
                    Task.campaign_id == campaign_id,
                    Task.skill_name == "ingest.csv_upload",
                )
            )
        ).scalar_one()
    assert task.status == TaskStatus.succeeded
    assert task.input_data["filename"] == "partial.csv"
    assert task.output_data["audience_id"] == str(audience_id)
    assert task.output_data["imported"] == 1
    assert task.started_at is not None
    assert task.completed_at is not None
    assert task.completed_at >= task.started_at


async def test_ingest_jobs_endpoint_lists_csv_upload(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, _, campaign_id = campaign_and_marketer
    # Two uploads to verify ordering.
    for filename in ("first.csv", "second.csv"):
        files = {"file": (filename, b"email\nseed@x.test\n", "text/csv")}
        await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)

    jobs = await client_marketer.get("/api/ingest/jobs")
    assert jobs.status_code == 200
    body = jobs.json()
    assert body["total"] >= 2
    names = [item["input_data"]["filename"] for item in body["items"]]
    # Newest first.
    assert names[:2] == ["second.csv", "first.csv"]
    # Each job carries the W13 shape:
    first = body["items"][0]
    assert first["skill_name"] == "ingest.csv_upload"
    assert first["status"] == "succeeded"
    assert first["duration_ms"] is not None and first["duration_ms"] >= 0
    assert "imported" in first["output_data"]


async def test_ingest_jobs_filter_by_status(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, _, campaign_id = campaign_and_marketer
    files = {"file": ("ok.csv", b"email\nseed@x.test\n", "text/csv")}
    await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    succeeded = await client_marketer.get("/api/ingest/jobs?status=succeeded")
    failed = await client_marketer.get("/api/ingest/jobs?status=failed")
    assert succeeded.status_code == 200 and succeeded.json()["total"] >= 1
    assert failed.status_code == 200 and failed.json()["total"] == 0


async def test_ingest_jobs_pagination(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, _, campaign_id = campaign_and_marketer
    for _ in range(3):
        await client_marketer.post(
            f"/api/campaigns/{campaign_id}/audiences/upload",
            files={"file": ("seed.csv", b"email\na@x.test\n", "text/csv")},
        )
    resp = await client_marketer.get("/api/ingest/jobs?limit=2")
    body = resp.json()
    assert body["limit"] == 2
    assert len(body["items"]) == 2


# -- Audience detail / freshness ---------------------------------------------


async def test_audience_detail_returns_freshness_summary(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    files = {"file": ("seed.csv", b"email\nfresh@x.test\nold@x.test\n", "text/csv")}
    upload = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences/upload", files=files
    )
    audience_id = uuid.UUID(upload.json()["audience_id"])

    # Backdate one member's fetched_at past the 30-day TTL.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(
            update(AudienceMember)
            .where(
                AudienceMember.audience_id == audience_id,
                AudienceMember.external_id == "old@x.test",
            )
            .values(fetched_at=datetime.now(UTC) - timedelta(days=60))
        )

    detail = await client_marketer.get(f"/api/audiences/{audience_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["freshness"]["member_count"] == 2
    assert body["freshness"]["stale_member_count"] == 1
    assert body["freshness"]["ttl_days"] == 30


async def test_audience_detail_null_fetched_at_counts_as_stale(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    files = {"file": ("seed.csv", b"email\nnull@x.test\n", "text/csv")}
    upload = await client_marketer.post(
        f"/api/campaigns/{campaign_id}/audiences/upload", files=files
    )
    audience_id = uuid.UUID(upload.json()["audience_id"])

    # Simulate a pre-W13 row by nulling the provenance.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(
            update(AudienceMember)
            .where(AudienceMember.audience_id == audience_id)
            .values(fetched_at=None, source=None)
        )

    detail = await client_marketer.get(f"/api/audiences/{audience_id}")
    body = detail.json()
    assert body["freshness"]["member_count"] == 1
    assert body["freshness"]["stale_member_count"] == 1


async def test_audience_detail_unknown_id_returns_404(
    client_marketer: httpx.AsyncClient,
) -> None:
    resp = await client_marketer.get(f"/api/audiences/{uuid.uuid4()}")
    assert resp.status_code == 404

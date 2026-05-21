"""W12 — CSV upload + validation + dedup (E01-S02, E01-S03)."""

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.audiences.csv_upload import parse_csv
from app.db.enums import CampaignType, UserRole
from app.db.models import AppUser, Audience, AudienceMember, Campaign, Tenant

# -- parse_csv (pure unit tests, no DB) --------------------------------------


def test_parse_csv_valid_rows() -> None:
    csv_text = (
        "email,first_name,last_name,company,country,tags\n"
        "ada@example.com,Ada,Lovelace,Acme,GB,founder\n"
        "alan@example.com,Alan,Turing,Bletchley,GB,researcher,vip\n"
    )
    result = parse_csv(csv_text)
    assert result.total_rows == 2
    assert len(result.valid) == 2
    assert result.errors == []
    assert result.valid[0].external_id == "ada@example.com"
    assert result.valid[1].payload["country"] == "GB"


def test_parse_csv_missing_email_column_short_circuits() -> None:
    csv_text = "name,company\nAlice,Acme\n"
    result = parse_csv(csv_text)
    assert result.valid == []
    assert len(result.errors) == 1
    assert result.errors[0].reason == "missing_required_header"


def test_parse_csv_missing_email_value() -> None:
    csv_text = "email,first_name\n,Alice\nbob@example.com,Bob\n"
    result = parse_csv(csv_text)
    assert [r.external_id for r in result.valid] == ["bob@example.com"]
    assert len(result.errors) == 1
    assert result.errors[0].reason == "missing_required_field"
    assert result.errors[0].row == 2


def test_parse_csv_invalid_email_format() -> None:
    csv_text = "email\nnot-an-email\nok@example.com\n"
    result = parse_csv(csv_text)
    assert [r.external_id for r in result.valid] == ["ok@example.com"]
    assert any(e.reason == "invalid_email_format" for e in result.errors)


def test_parse_csv_duplicate_within_file_first_wins() -> None:
    csv_text = (
        "email,first_name\ndup@example.com,First\nother@example.com,Other\nDUP@example.com,Second\n"
    )
    result = parse_csv(csv_text)
    emails = [r.external_id for r in result.valid]
    assert emails == ["dup@example.com", "other@example.com"]
    dup_errors = [e for e in result.errors if e.reason == "duplicate_in_file"]
    assert len(dup_errors) == 1
    assert dup_errors[0].value == "dup@example.com"
    assert dup_errors[0].row == 4


def test_parse_csv_field_too_long() -> None:
    huge = "x" * 201
    csv_text = f"email,first_name\nlong@example.com,{huge}\n"
    result = parse_csv(csv_text)
    assert result.valid == []
    assert result.errors[0].reason == "field_too_long"
    assert result.errors[0].field == "first_name"


def test_parse_csv_invalid_country() -> None:
    csv_text = "email,country\nok@example.com,UnitedKingdom\n"
    result = parse_csv(csv_text)
    assert result.valid == []
    assert result.errors[0].reason == "invalid_country_code"


def test_parse_csv_extra_columns_are_dropped() -> None:
    csv_text = "email,first_name,slack_handle,company\na@example.com,A,@ada,Acme\n"
    result = parse_csv(csv_text)
    assert len(result.valid) == 1
    # `slack_handle` isn't in KNOWN_FIELDS so it's dropped from the payload.
    assert "slack_handle" not in result.valid[0].payload
    assert result.valid[0].payload["company"] == "Acme"


def test_parse_csv_caps_at_max_rows() -> None:
    rows = "\n".join(f"u{i}@example.com" for i in range(15))
    csv_text = f"email\n{rows}\n"
    result = parse_csv(csv_text, max_rows=10)
    assert len(result.valid) == 10
    assert any(e.reason == "file_exceeds_max_rows" for e in result.errors)


# -- /api/campaigns/{id}/audiences/upload ------------------------------------


@pytest.fixture
async def campaign_and_marketer(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, AppUser, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"csv-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        user = AppUser(
            tenant_id=tenant.id,
            email=f"marketer-{uuid.uuid4().hex[:6]}@csv.test",
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
            objective="CSV upload demo",
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


async def test_upload_happy_path_creates_audience_and_members(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    csv_text = (
        "email,first_name,last_name,country,tags\n"
        "ada@example.com,Ada,Lovelace,GB,founder\n"
        "alan@example.com,Alan,Turing,GB,researcher\n"
    )
    files = {"file": ("seed.csv", csv_text.encode(), "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"] == {
        "total_rows": 2,
        "imported": 2,
        "skipped_duplicate": 0,
        "failed": 0,
    }
    assert body["audience_name"].startswith("CSV upload")

    audience_id = uuid.UUID(body["audience_id"])
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
        emails = sorted(m.external_id for m in members)
    assert emails == ["ada@example.com", "alan@example.com"]


async def test_upload_partial_failures_still_imports_good_rows(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    csv_text = (
        "email,first_name\n"
        ",NoEmail\n"  # missing required field
        "bad@@example,Broken\n"  # invalid format
        "ok@example.com,Good\n"  # valid
        "ok@example.com,Dup\n"  # duplicate
    )
    files = {"file": ("seed.csv", csv_text.encode(), "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"]["total_rows"] == 4
    assert body["summary"]["imported"] == 1
    assert body["summary"]["skipped_duplicate"] == 1
    assert body["summary"]["failed"] == 2
    reasons = [e["reason"] for e in body["errors"]]
    assert "missing_required_field" in reasons
    assert "invalid_email_format" in reasons
    assert "duplicate_in_file" in reasons

    audience_id = uuid.UUID(body["audience_id"])
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
    assert [m.external_id for m in members] == ["ok@example.com"]


async def test_upload_with_bad_header_returns_400_no_audience_created(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, _, campaign_id = campaign_and_marketer
    files = {"file": ("seed.csv", b"name,company\nAlice,Acme\n", "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_required_header"

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audiences = (
            (await session.execute(select(Audience).where(Audience.campaign_id == campaign_id)))
            .scalars()
            .all()
        )
    assert audiences == []


async def test_upload_records_provenance_in_segment_criteria(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    _, user, campaign_id = campaign_and_marketer
    files = {"file": ("acme-q3.csv", b"email\nadmin@acme.test\n", "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    audience_id = uuid.UUID(resp.json()["audience_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audience = await session.get(Audience, audience_id)
        assert audience is not None
        criteria = audience.segment_criteria
        assert criteria["source"] == "csv"
        assert criteria["filename"] == "acme-q3.csv"
        assert criteria["uploaded_by"] == str(user.id)
        assert audience.actual_size == 1
        assert audience.refreshed_at is not None


async def test_upload_unknown_campaign_returns_404(
    client_marketer: httpx.AsyncClient,
) -> None:
    files = {"file": ("seed.csv", b"email\na@b.test\n", "text/csv")}
    resp = await client_marketer.post(
        f"/api/campaigns/{uuid.uuid4()}/audiences/upload", files=files
    )
    assert resp.status_code == 404


async def test_upload_requires_marketer(
    override_api_db,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
    db_engine: AsyncEngine,
) -> None:
    tenant_id, _marketer, campaign_id = campaign_and_marketer
    # Build a viewer in the same tenant.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        viewer = AppUser(
            tenant_id=tenant_id,
            email=f"viewer-{uuid.uuid4().hex[:6]}@csv.test",
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
            files = {"file": ("seed.csv", b"email\na@b.test\n", "text/csv")}
            resp = await c.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_upload_non_utf8_returns_400(
    client_marketer: httpx.AsyncClient,
    campaign_and_marketer: tuple[uuid.UUID, AppUser, uuid.UUID],
) -> None:
    _, _, campaign_id = campaign_and_marketer
    files = {"file": ("seed.csv", b"\xff\xfe\x00bad bytes", "text/csv")}
    resp = await client_marketer.post(f"/api/campaigns/{campaign_id}/audiences/upload", files=files)
    assert resp.status_code == 400
    assert "UTF-8" in resp.json()["detail"]

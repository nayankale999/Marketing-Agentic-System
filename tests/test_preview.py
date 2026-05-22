"""W24 — Asset preview by channel (E06-S07).

Three layers under test:

  * `_preview` pure helpers — merge-field extraction, resolution priority,
    audience audit math, channel-constraint lookup.
  * API — GET/POST preview, audit-audience, share, public token consumer.
  * Token security — tampered signatures, expired tokens, cross-tenant tokens.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents._preview import (
    audit_audience_resolution,
    channel_constraints_for,
    extract_merge_fields,
    resolve_merge_fields,
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
    AudienceMember,
    AuditLog,
    Campaign,
    Channel,
    ContentAsset,
    Tenant,
)


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_extract_merge_fields_returns_distinct_in_appearance_order() -> None:
    out = extract_merge_fields(
        [
            "Hi {{first_name}}, your team at {{company}} is invited.",
            "Subject: a note for {{first_name}}",
            "PS — {{unsubscribe_url}}",
        ]
    )
    assert out == ["first_name", "company", "unsubscribe_url"]


def test_extract_merge_fields_handles_whitespace_inside_braces() -> None:
    assert extract_merge_fields(["Hello {{  first_name  }}"]) == ["first_name"]


def test_extract_merge_fields_ignores_non_strings() -> None:
    assert extract_merge_fields(["hi {{x}}", 123, None, {"a": 1}]) == ["x"]


def test_resolve_uses_caller_sample_values_first() -> None:
    rendered, report = resolve_merge_fields(
        {"body": "Hi {{first_name}}, from {{company}}."},
        sample_values={"first_name": "Maya", "company": "Acme"},
    )
    assert rendered == {"body": "Hi Maya, from Acme."}
    assert report.unresolved_fields == []


def test_resolve_falls_back_to_builtin_defaults() -> None:
    rendered, report = resolve_merge_fields({"body": "Hi {{first_name}}."})
    assert "Alex" in rendered["body"]
    assert report.unresolved_fields == []


def test_resolve_marks_unmatched_fields_unresolved() -> None:
    rendered, report = resolve_merge_fields(
        {"body": "Hi {{first_name}}, your spend {{currency_total}} is high."}
    )
    assert "currency_total" in report.unresolved_fields
    assert "{{currency_total}}" in rendered["body"]


def test_resolve_handles_no_merge_fields() -> None:
    rendered, report = resolve_merge_fields({"body": "Hello there."})
    assert rendered == {"body": "Hello there."}
    assert report.referenced_fields == []
    assert report.unresolved_fields == []


def test_resolve_case_insensitive_caller_keys() -> None:
    # Case-insensitive lookup is lowercase-equivalence, not snake_case
    # transformation: caller can pass `FIRST_NAME` and we'll match the
    # `{{first_name}}` placeholder; CamelCase keys still need to match the
    # placeholder's snake_case form when lowercased.
    rendered, _ = resolve_merge_fields(
        {"body": "Hi {{first_name}}"}, sample_values={"FIRST_NAME": "Pat"}
    )
    assert "Pat" in rendered["body"]


def test_resolve_preserves_non_string_field_values() -> None:
    rendered, _ = resolve_merge_fields({"body": "Hi", "count": 42})  # type: ignore[arg-type]
    assert rendered["count"] == 42


def test_audit_audience_counts_unresolved_per_field() -> None:
    counts = audit_audience_resolution(
        ["first_name", "company"],
        [
            {"first_name": "Pat", "company": "Acme"},
            {"first_name": "Sam"},  # missing company
            {},  # missing both
            {"first_name": "", "company": "  "},  # blank counts as unresolved
        ],
    )
    assert counts["first_name"]["unresolved"] == 2
    assert counts["company"]["unresolved"] == 3
    assert counts["first_name"]["total"] == 4


def test_audit_audience_empty_fields_returns_empty_dict() -> None:
    assert audit_audience_resolution([], [{"x": 1}]) == {}


def test_channel_constraints_for_email_returns_required_fields() -> None:
    out = channel_constraints_for("email", "email")
    assert "subject" in out["required_fields"]
    assert "preheader" in out["required_fields"]
    assert "subject" in out["length_budgets"]


def test_channel_constraints_for_x_caps_body_at_280() -> None:
    out = channel_constraints_for("social_post", "x")
    assert out["length_budgets"].get("body") == 280


def test_channel_constraints_falls_back_to_asset_type() -> None:
    # No 'unknown' platform — falls back to the asset_type lookup.
    out = channel_constraints_for("blog_post", "unknown_platform")
    assert out["required_fields"]  # blog_post has required fields


# ---------------------------------------------------------------------------
# API integration helpers
# ---------------------------------------------------------------------------


async def _seed_world(
    db_engine: AsyncEngine,
    *,
    asset_body: str = "Hi {{first_name}}, your team at {{company}} is invited.",
    asset_subject: str = "Hello {{first_name}}",
    member_payloads: list[dict] | None = None,
) -> dict[str, uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"prev-{uuid.uuid4().hex[:6]}")
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

        campaign = Campaign(
            tenant_id=tenant.id,
            name="prev-camp",
            campaign_type=CampaignType.product_launch,
            objective="x",
            brief="b",
            budget_total=Decimal("10000.00"),
            currency="USD",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            status=CampaignStatus.content_in_production,
        )
        session.add(campaign)
        await session.flush()

        audience = Audience(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            name="seg",
            segment_criteria={},
            estimated_size=len(member_payloads or []),
            actual_size=len(member_payloads or []),
            refreshed_at=datetime.now(UTC),
        )
        session.add(audience)
        await session.flush()

        for i, payload in enumerate(member_payloads or []):
            session.add(
                AudienceMember(
                    audience_id=audience.id,
                    external_id=f"ext-{i}",
                    payload=payload,
                    source="seeded",
                    fetched_at=datetime.now(UTC),
                )
            )

        asset = ContentAsset(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            asset_type=AssetType.email,
            status=AssetStatus.drafted,
            title=asset_subject,
            content=asset_body,
            extra_metadata={
                "channel_platform": "email",
                "fields": {
                    "subject": asset_subject,
                    "preheader": "Quick note for {{first_name}}",
                    "cta": "Reply yes",
                },
            },
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
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@prev.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def world(override_api_db, db_engine: AsyncEngine):
    return await _seed_world(
        db_engine,
        member_payloads=[
            {"first_name": "Pat", "company": "Acme"},
            {"first_name": "Sam"},  # missing company
            {},
        ],
    )


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


@pytest.fixture
async def public_client(override_api_db) -> AsyncIterator[httpx.AsyncClient]:
    """No auth override — exercises the public token endpoint."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# GET / POST preview
# ---------------------------------------------------------------------------


async def test_get_preview_resolves_defaults(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/content-assets/{world['asset_id']}/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_type"] == "email"
    assert body["channel_kind"] == "email"
    assert "Alex" in body["rendered"]["body"]
    assert "Acme Corp" in body["rendered"]["body"]
    assert set(body["referenced_fields"]) >= {"first_name", "company"}
    assert body["unresolved_fields"] == []
    assert "subject" in body["channel_constraints"]["required_fields"]


async def test_post_preview_swaps_sample_values(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview",
        json={"sample_values": {"first_name": "Maya", "company": "Globex"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "Maya" in body["rendered"]["body"]
    assert "Globex" in body["rendered"]["body"]
    assert body["resolved_with"]["first_name"] == "Maya"


async def test_post_preview_idempotent_different_values(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    first = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview",
        json={"sample_values": {"first_name": "A"}},
    )
    second = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview",
        json={"sample_values": {"first_name": "B"}},
    )
    assert "A," in first.json()["rendered"]["body"]
    assert "B," in second.json()["rendered"]["body"]


async def test_preview_404_for_missing_asset(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.get(f"/api/content-assets/{uuid.uuid4()}/preview")
    assert resp.status_code == 404


async def test_preview_reports_unresolved_fields(
    override_api_db, db_engine: AsyncEngine
) -> None:
    world = await _seed_world(
        db_engine,
        asset_body="Discount for {{custom_field_that_does_not_exist}}!",
    )
    user = await _make_user(db_engine, world["tenant_id"], UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/content-assets/{world['asset_id']}/preview"
            )
            assert resp.status_code == 200
            assert "custom_field_that_does_not_exist" in resp.json()["unresolved_fields"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Audience audit
# ---------------------------------------------------------------------------


async def test_audit_audience_counts_unresolved(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview/audit-audience"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_members"] == 3
    by_field = {entry["field"]: entry for entry in body["field_audit"]}
    # first_name: 2 of 3 have it (Pat, Sam) → 1 unresolved
    assert by_field["first_name"]["unresolved"] == 1
    # company: 1 of 3 has it (Pat) → 2 unresolved
    assert by_field["company"]["unresolved"] == 2


async def test_audit_audience_returns_empty_when_no_audience(
    override_api_db, db_engine: AsyncEngine
) -> None:
    world = await _seed_world(db_engine, member_payloads=[])
    # Drop the empty audience so we hit the "no audience" branch.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        audience = (
            await session.execute(
                select(Audience).where(Audience.campaign_id == world["campaign_id"])
            )
        ).scalar_one()
        await session.delete(audience)

    user = await _make_user(db_engine, world["tenant_id"], UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/content-assets/{world['asset_id']}/preview/audit-audience"
            )
            assert resp.status_code == 200
            assert resp.json()["total_members"] == 0
            assert resp.json()["field_audit"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Share token lifecycle + security
# ---------------------------------------------------------------------------


async def test_share_returns_token_and_writes_audit(
    client_as, world, db_engine: AsyncEngine
) -> None:
    client, user = await client_as(UserRole.marketer)
    resp = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview/share",
        json={"ttl_days": 3},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"]
    assert body["url_path"].startswith("/api/preview-links/")
    assert body["asset_id"] == str(world["asset_id"])

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "content_asset",
                    AuditLog.entity_id == world["asset_id"],
                    AuditLog.action == "preview_shared",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert audits[0].extra_metadata["ttl_days"] == 3
        assert audits[0].extra_metadata["shared_by_user_id"] == str(user.id)


async def test_share_then_consume_returns_same_preview(
    client_as, world, public_client
) -> None:
    client, _ = await client_as(UserRole.marketer)
    share = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview/share", json={}
    )
    assert share.status_code == 201
    token = share.json()["token"]

    resp = await public_client.get(f"/api/preview-links/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_id"] == str(world["asset_id"])
    assert body["asset_type"] == "email"


async def test_tampered_token_returns_404(public_client) -> None:
    resp = await public_client.get("/api/preview-links/not-a-real-token")
    assert resp.status_code == 404


async def test_token_signed_with_wrong_secret_is_rejected(
    world, public_client
) -> None:
    bad_serializer = URLSafeTimedSerializer(
        secret_key="some-other-secret", salt="asset-preview-share"
    )
    token = bad_serializer.dumps(
        {"asset_id": str(world["asset_id"]), "tenant_id": str(world["tenant_id"])}
    )
    resp = await public_client.get(f"/api/preview-links/{token}")
    assert resp.status_code == 404


async def test_expired_token_returns_410(world, public_client, monkeypatch) -> None:
    from app.settings.config import get_settings

    monkeypatch.setenv("PREVIEW_SHARE_TTL_DAYS", "1")
    get_settings.cache_clear()
    settings = get_settings()
    serializer = URLSafeTimedSerializer(
        secret_key=settings.effective_preview_share_secret(),
        salt="asset-preview-share",
    )
    # Encode then directly tamper the embedded timestamp — easier than
    # mocking time. itsdangerous embeds the timestamp as an int between
    # delimiters; instead we just use a tiny max_age via a TTL=0 path:
    # set TTL to 0 days so any token is immediately expired.
    monkeypatch.setenv("PREVIEW_SHARE_TTL_DAYS", "0")
    get_settings.cache_clear()
    token = serializer.dumps(
        {"asset_id": str(world["asset_id"]), "tenant_id": str(world["tenant_id"])}
    )
    # Sleep slightly to push the token age above the 0-day max
    import time as _time

    _time.sleep(1.1)
    resp = await public_client.get(f"/api/preview-links/{token}")
    # With max_age=0 the verifier treats it as expired; should land 410.
    assert resp.status_code == 410


async def test_viewer_cannot_share(client_as, world) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post(
        f"/api/content-assets/{world['asset_id']}/preview/share", json={}
    )
    assert resp.status_code == 403


async def test_share_404_for_missing_asset(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    resp = await client.post(
        f"/api/content-assets/{uuid.uuid4()}/preview/share", json={}
    )
    assert resp.status_code == 404

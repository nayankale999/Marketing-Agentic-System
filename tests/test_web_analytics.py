"""W16 — Plausible connector + UTM attribution + ingest writer (E12-S05, E01-S04)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import CampaignType, EventKind, UserRole
from app.db.models import AnalyticEvent, AppUser, Campaign, Tenant
from app.integrations.web_analytics.attribution import resolve_campaign_by_utm
from app.integrations.web_analytics.base import WebAnalyticsEvent
from app.integrations.web_analytics.ingest import ingest_events
from app.integrations.web_analytics.plausible import PlausibleConnector


@pytest.fixture
async def seeded(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID, AppUser]:
    """tenant + 'Spring Launch' campaign + admin user."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"wa-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        admin = AppUser(
            tenant_id=tenant.id,
            email=f"admin-{uuid.uuid4().hex[:6]}@wa.test",
            role=UserRole.admin,
            is_active=True,
        )
        session.add(admin)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            owner_id=admin.id,
            name="Spring Launch",
            campaign_type=CampaignType.product_launch,
            objective="W16",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
        )
        session.add(campaign)
        await session.flush()
        return tenant.id, campaign.id, admin


# ---------------------------------------------------------------------------
# UTM attribution
# ---------------------------------------------------------------------------


async def test_resolve_campaign_by_utm_matches_name_case_insensitive(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    tenant_id, campaign_id, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        match = await resolve_campaign_by_utm(
            session, tenant_id=tenant_id, utm_campaign="spring launch"
        )
        assert match == campaign_id

        miss = await resolve_campaign_by_utm(
            session, tenant_id=tenant_id, utm_campaign="autumn drop"
        )
        assert miss is None

        nothing = await resolve_campaign_by_utm(session, tenant_id=tenant_id, utm_campaign=None)
        assert nothing is None


async def test_resolve_campaign_by_utm_is_tenant_scoped(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    """Campaign in another tenant must not match."""
    _, _campaign_id, _ = seeded
    other_tenant = uuid.uuid4()
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        match = await resolve_campaign_by_utm(
            session, tenant_id=other_tenant, utm_campaign="Spring Launch"
        )
        assert match is None


# ---------------------------------------------------------------------------
# ingest_events
# ---------------------------------------------------------------------------


def _make_event(
    *,
    pid: str,
    utm: str | None,
    metric: float = 100.0,
    kind: EventKind = EventKind.impression,
) -> WebAnalyticsEvent:
    return WebAnalyticsEvent(
        provider_event_id=pid,
        event_type=kind,
        metric_value=metric,
        event_at=datetime.now(UTC),
        utm_campaign=utm,
    )


async def test_ingest_persists_attributed_events(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    tenant_id, campaign_id, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        summary = await ingest_events(
            session,
            tenant_id=tenant_id,
            events=[
                _make_event(pid="ev-1", utm="Spring Launch"),
                _make_event(pid="ev-2", utm="spring launch", metric=50.0),
            ],
        )
    assert summary.imported == 2
    assert summary.duplicates == 0
    assert summary.unattributed == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(AnalyticEvent).where(AnalyticEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    assert {r.campaign_id for r in rows} == {campaign_id}
    assert {r.provider_event_id for r in rows} == {"ev-1", "ev-2"}
    assert {r.payload["utm_campaign"] for r in rows} == {"Spring Launch", "spring launch"}


async def test_ingest_unattributed_event_stores_null_campaign(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    tenant_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        summary = await ingest_events(
            session,
            tenant_id=tenant_id,
            events=[_make_event(pid="ev-unk", utm="No Such Campaign")],
        )
    assert summary.imported == 1
    assert summary.unattributed == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(
                select(AnalyticEvent).where(AnalyticEvent.provider_event_id == "ev-unk")
            )
        ).scalar_one()
    assert row.campaign_id is None
    assert row.payload["utm_campaign"] == "No Such Campaign"


async def test_ingest_dedupes_on_provider_event_id(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    tenant_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await ingest_events(
            session, tenant_id=tenant_id, events=[_make_event(pid="dup-1", utm="Spring Launch")]
        )
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        summary = await ingest_events(
            session,
            tenant_id=tenant_id,
            events=[
                _make_event(pid="dup-1", utm="Spring Launch"),  # already in DB
                _make_event(pid="dup-2", utm="Spring Launch"),  # new
            ],
        )
    assert summary.imported == 1
    assert summary.duplicates == 1

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(AnalyticEvent)
                .where(
                    AnalyticEvent.tenant_id == tenant_id,
                    AnalyticEvent.provider_event_id.in_(["dup-1", "dup-2"]),
                )
            )
        ).scalar_one()
    assert count == 2


async def test_ingest_empty_list_is_noop(
    db_engine: AsyncEngine, seeded: tuple[uuid.UUID, uuid.UUID, AppUser]
) -> None:
    tenant_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        summary = await ingest_events(session, tenant_id=tenant_id, events=[])
    assert summary.imported == 0
    assert summary.duplicates == 0


# ---------------------------------------------------------------------------
# PlausibleConnector (respx-mocked)
# ---------------------------------------------------------------------------


@respx.mock
async def test_plausible_connector_emits_two_events_per_bucket() -> None:
    respx.get("https://plausible.io/api/v1/stats/breakdown").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"visit:utm_campaign": "Spring Launch", "pageviews": 240, "visitors": 88},
                    {"visit:utm_campaign": None, "pageviews": 12, "visitors": 7},
                ]
            },
        )
    )
    connector = PlausibleConnector(api_key="api-key-test", site_id="acme.test")
    until = datetime(2026, 5, 21, tzinfo=UTC)
    since = until - timedelta(days=7)
    events = await connector.fetch_events(since=since, until=until)

    # Two buckets x {pageviews,visitors} = 4 events.
    assert len(events) == 4
    by_pid = {e.provider_event_id: e for e in events}
    spring = [e for e in events if e.utm_campaign == "Spring Launch"]
    assert {e.event_type for e in spring} == {EventKind.impression, EventKind.click}
    assert {e.metric_value for e in spring} == {240.0, 88.0}
    # provider_event_id is deterministic for dedup
    assert any("Spring Launch:pageviews" in pid for pid in by_pid)
    assert any("none:pageviews" in pid for pid in by_pid)


# ---------------------------------------------------------------------------
# /api/integrations/plausible/sync
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_client(
    override_api_db,
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator:
    monkeypatch.setenv("PLAUSIBLE_API_KEY", "k-test")
    monkeypatch.setenv("PLAUSIBLE_SITE_ID", "acme.test")
    from app.settings.config import get_settings

    get_settings.cache_clear()

    _, _, user = seeded
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        try:
            yield c
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            get_settings.cache_clear()


@respx.mock
async def test_sync_endpoint_fetches_and_persists(
    admin_client: httpx.AsyncClient,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser],
    db_engine: AsyncEngine,
) -> None:
    tenant_id, campaign_id, _ = seeded
    respx.get("https://plausible.io/api/v1/stats/breakdown").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"visit:utm_campaign": "Spring Launch", "pageviews": 100, "visitors": 30},
                ]
            },
        )
    )
    resp = await admin_client.post("/api/integrations/plausible/sync", json={"days": 3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fetched"] == 2  # 1 bucket x 2 metrics
    assert body["imported"] == 2
    assert body["duplicates"] == 0
    assert body["unattributed"] == 0

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(AnalyticEvent).where(AnalyticEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    assert {r.campaign_id for r in rows} == {campaign_id}


@respx.mock
async def test_sync_endpoint_idempotent_on_same_window(
    admin_client: httpx.AsyncClient,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser],
    db_engine: AsyncEngine,
) -> None:
    """Second sync over the same date range -> duplicates, no new rows."""
    _tenant_id, _, _ = seeded
    respx.get("https://plausible.io/api/v1/stats/breakdown").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"visit:utm_campaign": "Spring Launch", "pageviews": 50, "visitors": 12},
                ]
            },
        )
    )
    first = (await admin_client.post("/api/integrations/plausible/sync", json={"days": 1})).json()
    assert first["imported"] == 2

    # Re-run with the same window — connector's provider_event_id is keyed on the
    # date pair, so the window must be stable for dedup to fire.
    second = (await admin_client.post("/api/integrations/plausible/sync", json={"days": 1})).json()
    assert second["fetched"] == 2
    assert second["imported"] == 0
    assert second["duplicates"] == 2


async def test_sync_endpoint_returns_503_when_unconfigured(
    override_api_db,
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    monkeypatch.delenv("PLAUSIBLE_SITE_ID", raising=False)
    from app.settings.config import get_settings

    get_settings.cache_clear()

    _, _, admin = seeded
    app.dependency_overrides[get_current_user] = lambda: admin
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/integrations/plausible/sync")
            assert resp.status_code == 503
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        get_settings.cache_clear()


async def test_sync_endpoint_requires_admin(
    override_api_db,
    db_engine: AsyncEngine,
    seeded: tuple[uuid.UUID, uuid.UUID, AppUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAUSIBLE_API_KEY", "k-test")
    monkeypatch.setenv("PLAUSIBLE_SITE_ID", "acme.test")
    from app.settings.config import get_settings

    get_settings.cache_clear()

    tenant_id, _, _ = seeded
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        viewer = AppUser(
            tenant_id=tenant_id,
            email=f"viewer-{uuid.uuid4().hex[:6]}@wa.test",
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
            resp = await c.post("/api/integrations/plausible/sync")
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        get_settings.cache_clear()

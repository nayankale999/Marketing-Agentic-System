"""Slice 2 end-to-end demo (W10 -> W16).

Drives every Slice-2 endpoint through one httpx client and prints what
each layer wrote. Plausible is respx-mocked because we don't ship real
credentials. The HubSpot OAuth flow has its own demo and isn't repeated
here (it's covered by tests + the W11 setup notes).

Steps:
  1. Reset prior demo state for tenant 'slice2.demo'.
  2. Seed tenant + an admin user (so we can hit the admin-only Plausible
     sync endpoint too) + a 'Spring Launch' campaign via POST /api/campaigns.
  3. Upload a CSV of contacts via POST /api/campaigns/{id}/audiences/upload
     and show the summary.
  4. Run POST /api/audiences/estimate with a country+tag rule.
  5. Enqueue a criteria-driven materialisation via POST
     /api/campaigns/{id}/audiences, run one worker iteration, show the
     resulting audience + members.
  6. POST /api/integrations/plausible/sync with respx-mocked Plausible
     responses; show attributed vs unattributed events.
  7. Dump tasks / audiences / audience_members / analytic_events / audit_log.

Run:    .venv/bin/python -m scripts.slice2_demo
Prereq: docker compose stack up + `alembic upgrade head`.
"""

import asyncio
import json
import uuid
from datetime import date, timedelta

import httpx
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.app import app
from app.api.deps import get_current_user, get_db, get_tenant_db
from app.audit import register_listeners
from app.db.models import (
    AnalyticEvent,
    AppUser,
    Audience,
    AudienceMember,
    AuditLog,
    Campaign,
    Task,
    Tenant,
)
from app.db.session import SessionLocal, engine, set_tenant_context
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.worker import run_once
from app.tools import register_builtin_tools

DASH = "─" * 78
DEMO_TENANT = "slice2.demo"


def heading(title: str) -> None:
    print(f"\n{DASH}\n{title}\n{DASH}")


def short(value: object, n: int = 70) -> str:
    s = str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


# --- DB helpers --------------------------------------------------------------


async def reset_demo_state() -> None:
    """Erase prior runs so re-running is idempotent."""
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM agent_log WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM analytic_event WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM audience_member WHERE audience_id IN "
                "(SELECT id FROM audience WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE name = :name))"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM audience WHERE tenant_id IN (SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text("DELETE FROM task WHERE tenant_id IN (SELECT id FROM tenant WHERE name = :name)"),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text("DELETE FROM agent WHERE tenant_id IN (SELECT id FROM tenant WHERE name = :name)"),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM campaign WHERE tenant_id IN (SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM app_user WHERE tenant_id IN (SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(
            text(
                "DELETE FROM audit_log WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE name = :name)"
            ),
            {"name": DEMO_TENANT},
        )
        await session.execute(text("DELETE FROM tenant WHERE name = :name"), {"name": DEMO_TENANT})


async def seed_tenant_and_admin() -> tuple[uuid.UUID, AppUser]:
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(name=DEMO_TENANT, oidc_hosted_domain="slice2.test")
        session.add(tenant)
        await session.flush()
        admin = AppUser(
            tenant_id=tenant.id,
            email="ops@slice2.test",
            display_name="Ops Admin",
            role="admin",
            is_active=True,
        )
        session.add(admin)
        await session.flush()
        await session.refresh(admin)
        return tenant.id, admin


# --- httpx client overrides --------------------------------------------------


def install_dep_overrides(user: AppUser) -> None:
    """Point /api routes at our session_maker + a fixed signed-in user."""
    test_session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with test_session_maker() as session:
            yield session

    async def _get_tenant_db():
        async with test_session_maker() as session:
            try:
                await set_tenant_context(session, user.tenant_id)
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_tenant_db] = _get_tenant_db


def remove_dep_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_tenant_db, None)


# --- main --------------------------------------------------------------------


async def main() -> None:
    register_listeners()
    register_builtin_tools()
    register_builtin_handlers()

    heading("Slice 2 end-to-end demo (W10 -> W16)")
    print(f"DB:      {engine.url}")
    print(f"Tenant:  {DEMO_TENANT}")

    await reset_demo_state()
    tenant_id, admin = await seed_tenant_and_admin()
    print(f"  tenant_id = {tenant_id}")
    print(f"  admin     = {admin.email} (role={admin.role.value})")

    install_dep_overrides(admin)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # ----------------------------------------------------------- W10
            heading("1. W10 — POST /api/campaigns  (create 'Spring Launch')")
            create_resp = await client.post(
                "/api/campaigns",
                json={
                    "name": "Spring Launch",
                    "campaign_type": "product_launch",
                    "objective": "Drive SMB awareness for the new tier",
                    "start_date": date.today().isoformat(),
                    "end_date": (date.today() + timedelta(days=45)).isoformat(),
                    "budget_total": "25000.00",
                    "currency": "EUR",
                    "brief": "Beta launch targeting US + GB SMB buyers",
                    "kpi_targets": {"primary": "MQLs", "target": 400},
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            campaign = create_resp.json()
            campaign_id = uuid.UUID(campaign["id"])
            print(f"  status: {create_resp.status_code}")
            print(f"  campaign_id: {campaign['id']}")
            print(f"  name: {campaign['name']!r}  status: {campaign['status']!r}")
            print(f"  budget: {campaign['budget_total']} {campaign['currency']}")

            # ----------------------------------------------------------- W12
            heading("2. W12 — POST /api/campaigns/{id}/audiences/upload  (CSV)")
            csv = (
                "email,first_name,last_name,country,tags\n"
                "ada@example.com,Ada,Lovelace,GB,vip\n"
                "bob@example.com,Bob,Smith,US,vip\n"
                "carol@example.com,Carol,Jones,US,\n"
                "dave@example.com,Dave,Burke,DE,blocked\n"
                ",,,,\n"  # missing email
                "bad@@example,Eve,X,US,\n"  # invalid email
                "ada@example.com,Dup,Lovelace,GB,\n"  # duplicate
            )
            upload_resp = await client.post(
                f"/api/campaigns/{campaign_id}/audiences/upload",
                files={"file": ("seed.csv", csv.encode(), "text/csv")},
            )
            assert upload_resp.status_code == 201, upload_resp.text
            upload = upload_resp.json()
            print(f"  status: {upload_resp.status_code}")
            print(f"  audience_id: {upload['audience_id']}")
            print(f"  summary: {upload['summary']}")
            for err in upload["errors"]:
                print(f"    row {err['row']}: {err['reason']} ({err.get('value') or '-'})")

            # ----------------------------------------------------------- W14
            heading("3. W14 — POST /api/audiences/estimate  (US + tag:vip, excluding blocked)")
            est_resp = await client.post(
                "/api/audiences/estimate",
                json={
                    "include": [
                        {"field": "country", "op": "in", "value": ["US", "GB"]},
                        {"field": "tags", "op": "has", "value": "vip"},
                    ],
                    "exclude": [{"field": "tags", "op": "has", "value": "blocked"}],
                },
            )
            assert est_resp.status_code == 200, est_resp.text
            print(f"  status: {est_resp.status_code}")
            print(f"  estimate: {est_resp.json()}")

            # ----------------------------------------------------------- W15
            heading(
                "4. W15 — POST /api/campaigns/{id}/audiences  (criteria-driven)\n"
                "    -> task enqueued, then run_once consumes it"
            )
            mat_resp = await client.post(
                f"/api/campaigns/{campaign_id}/audiences",
                json={
                    "name": "US + GB VIPs (auto)",
                    "criteria": {
                        "include": [
                            {"field": "country", "op": "in", "value": ["US", "GB"]},
                            {"field": "tags", "op": "has", "value": "vip"},
                        ],
                        "exclude": [{"field": "tags", "op": "has", "value": "blocked"}],
                    },
                },
            )
            assert mat_resp.status_code == 202, mat_resp.text
            queued = mat_resp.json()
            print(f"  enqueue: status={mat_resp.status_code}, task_id={queued['task_id']}")
            print(f"  skill: {queued['skill_name']}, task status: {queued['status']}")

            handled = await run_once(worker_id="slice2-demo-worker")
            print(f"  worker run_once handled a task: {handled}")

            async with SessionLocal() as session:
                materialised_task = await session.get(Task, uuid.UUID(queued["task_id"]))
                assert materialised_task is not None
                output = materialised_task.output_data
                materialised_audience_id = uuid.UUID(output["audience_id"])
                print(f"  task status after worker: {materialised_task.status.value}")
                print(f"  new audience_id: {output['audience_id']}")
                print(f"  member_count: {output['member_count']}")

            # ----------------------------------------------------------- W16
            heading("5. W16 — POST /api/integrations/plausible/sync  (respx-mocked)")
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://plausible.io/api/v1/stats/breakdown").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "visit:utm_campaign": "Spring Launch",
                                    "pageviews": 1240,
                                    "visitors": 412,
                                },
                                {
                                    "visit:utm_campaign": "Ghost Promo",
                                    "pageviews": 17,
                                    "visitors": 9,
                                },
                                {
                                    "visit:utm_campaign": None,
                                    "pageviews": 88,
                                    "visitors": 60,
                                },
                            ]
                        },
                    )
                )
                # Configure Plausible settings just for this call.
                import os

                os.environ["PLAUSIBLE_API_KEY"] = "demo-key"
                os.environ["PLAUSIBLE_SITE_ID"] = "slice2.test"
                from app.settings.config import get_settings

                get_settings.cache_clear()

                sync_resp = await client.post("/api/integrations/plausible/sync", json={"days": 7})
                assert sync_resp.status_code == 200, sync_resp.text
                sync = sync_resp.json()
            print(f"  status: {sync_resp.status_code}")
            print(f"  fetched: {sync['fetched']}, imported: {sync['imported']}")
            print(f"  duplicates: {sync['duplicates']}, unattributed: {sync['unattributed']}")

            # ----------------------------------------------------------- W13
            heading("6. W13 — GET /api/ingest/jobs  (operator dashboard)")
            jobs = await client.get("/api/ingest/jobs?limit=20")
            print(f"  status: {jobs.status_code}")
            for j in jobs.json()["items"]:
                print(
                    f"  [{j['status']:<9}] {j['skill_name']:<26} "
                    f"output={short(json.dumps(j['output_data']))}"
                )
    finally:
        remove_dep_overrides()

    # ----------------------------------------------------------- Final state
    heading("DB state for this tenant")
    async with SessionLocal() as session:
        n_campaigns = (
            await session.execute(
                select(func.count()).select_from(Campaign).where(Campaign.tenant_id == tenant_id)
            )
        ).scalar_one()
        n_audiences = (
            await session.execute(
                select(func.count()).select_from(Audience).where(Audience.tenant_id == tenant_id)
            )
        ).scalar_one()
        n_members = (
            await session.execute(
                select(func.count())
                .select_from(AudienceMember)
                .join(Audience, Audience.id == AudienceMember.audience_id)
                .where(Audience.tenant_id == tenant_id)
            )
        ).scalar_one()
        n_events = (
            await session.execute(
                select(func.count())
                .select_from(AnalyticEvent)
                .where(AnalyticEvent.tenant_id == tenant_id)
            )
        ).scalar_one()
        attributed = (
            await session.execute(
                select(func.count())
                .select_from(AnalyticEvent)
                .where(
                    AnalyticEvent.tenant_id == tenant_id,
                    AnalyticEvent.campaign_id.is_not(None),
                )
            )
        ).scalar_one()
        n_audit = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == tenant_id)
            )
        ).scalar_one()

    print(f"  campaigns:        {n_campaigns}")
    print(f"  audiences:        {n_audiences}  (seed CSV + materialised)")
    print(f"  audience_members: {n_members}")
    print(
        f"  analytic_events:  {n_events}  "
        f"(attributed={attributed}, unattributed={n_events - attributed})"
    )
    print(f"  audit_log rows:   {n_audit}")

    heading("Audiences (name, source, actual_size)")
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Audience)
                    .where(Audience.tenant_id == tenant_id)
                    .order_by(Audience.created_at)
                )
            )
            .scalars()
            .all()
        )
    for a in rows:
        src = a.segment_criteria.get("source", "criteria")
        print(f"  [{src:>8}] {a.name:<25} actual_size={a.actual_size}")

    heading("Sample materialised members (5 max)")
    async with SessionLocal() as session:
        members = (
            (
                await session.execute(
                    select(AudienceMember)
                    .where(AudienceMember.audience_id == materialised_audience_id)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    for m in members:
        print(f"  {m.external_id:<25} source={m.source!r}  payload={short(json.dumps(m.payload))}")

    heading("Analytic events for 'Spring Launch'")
    async with SessionLocal() as session:
        events = (
            (
                await session.execute(
                    select(AnalyticEvent)
                    .where(AnalyticEvent.tenant_id == tenant_id)
                    .order_by(AnalyticEvent.provider_event_id)
                )
            )
            .scalars()
            .all()
        )
    for e in events:
        utm = e.payload.get("utm_campaign", "<none>")
        attrib = "attributed" if e.campaign_id else "unattributed"
        print(f"  [{e.event_type.value:>10}] utm={utm!r:<20} metric={e.metric_value}  ({attrib})")

    heading("Done")
    print("Re-run with: cd ~/mas-demo/mas && uv run python -m scripts.slice2_demo")
    print(f"Wipe demo data: DELETE FROM tenant WHERE name = '{DEMO_TENANT}'")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Slice 1 end-to-end demo via the real HTTP surface.

Steps:
  1. Reset demo data; seed an `acme.test` tenant + a drafted campaign.
  2. Run the full OIDC handshake against the local oidc-mock:
       GET /api/auth/login -> 302 to oidc-mock authorize
       oidc-mock (interactiveLogin=false) auto-issues a code and redirects
       MAS callback exchanges code for token, JIT-provisions a viewer
  3. With the session cookie, hit:
       /api/me                       -> 200, profile of the JIT user
       /api/_protected/marketer      -> 403 (viewer can't reach marketer)
  4. Promote the JIT user to marketer via direct SQL.
  5. With the SAME session cookie:
       /api/_protected/marketer      -> 200
       POST /api/campaigns/{id}/transitions/echo_step  -> 200
  6. Wait for the worker to process the queued echo task.
  7. Dump task / audit_log / agent_log rows and a span count from the
     OTel collector.

Run:    .venv/bin/python -m scripts.slice1_e2e_demo
Prereq: `make infra` (postgres + mailpit + otel-collector + oidc-mock),
        `make migrate`, plus the app on :8001 and a worker process.
"""

import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta

import httpx
from sqlalchemy import select, text, update

from app.audit import register_listeners
from app.db.enums import CampaignType, UserRole
from app.db.models import AgentLog, AppUser, AuditLog, Campaign, Task, Tenant
from app.db.session import SessionLocal, engine
from app.orchestrator.handlers import register_builtin_handlers
from app.tools import register_builtin_tools

APP_BASE = "http://localhost:8001"  # must match OIDC_REDIRECT_URI host
DASH = "─" * 78
DOMAIN = "acme.test"


def heading(title: str) -> None:
    print(f"\n{DASH}\n{title}\n{DASH}")


def short(value: object, n: int = 64) -> str:
    s = str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


async def reset_demo_state() -> None:
    async with SessionLocal() as session, session.begin():
        await session.execute(text("DELETE FROM agent_log WHERE task_id IS NOT NULL"))
        await session.execute(
            text(
                "DELETE FROM task WHERE campaign_id IN "
                "(SELECT id FROM campaign WHERE name LIKE 'e2e-%')"
            )
        )
        await session.execute(text("DELETE FROM campaign WHERE name LIKE 'e2e-%'"))
        await session.execute(
            text(
                "DELETE FROM agent WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE oidc_hosted_domain = :d)"
            ),
            {"d": DOMAIN},
        )
        await session.execute(
            text(
                "DELETE FROM app_user WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE oidc_hosted_domain = :d)"
            ),
            {"d": DOMAIN},
        )
        await session.execute(
            text(
                "DELETE FROM audit_log WHERE tenant_id IN "
                "(SELECT id FROM tenant WHERE oidc_hosted_domain = :d)"
            ),
            {"d": DOMAIN},
        )
        await session.execute(
            text("DELETE FROM tenant WHERE oidc_hosted_domain = :d"), {"d": DOMAIN}
        )


async def seed_tenant_and_campaign() -> tuple[uuid.UUID, uuid.UUID]:
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(name="Acme Inc (demo)", oidc_hosted_domain=DOMAIN)
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name=f"e2e-spring-{uuid.uuid4().hex[:6]}",
            campaign_type=CampaignType.product_launch,
            objective="Slice 1 e2e demo",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
        )
        session.add(campaign)
        await session.flush()
        return tenant.id, campaign.id


async def fetch_app_user(email: str) -> AppUser | None:
    async with SessionLocal() as session:
        return (
            await session.execute(select(AppUser).where(AppUser.email == email))
        ).scalar_one_or_none()


async def promote_user_to_marketer(user_id: uuid.UUID) -> None:
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(AppUser).where(AppUser.id == user_id).values(role=UserRole.marketer)
        )


def otel_span_count() -> int:
    result = subprocess.run(["docker", "logs", "mas-otel"], capture_output=True, text=True)
    return result.stdout.count("Span #") + result.stderr.count("Span #")


async def main() -> None:
    register_listeners()
    register_builtin_tools()
    register_builtin_handlers()

    heading("Slice 1 end-to-end demo")
    print(f"App:      {APP_BASE}")
    print("OIDC:     http://localhost:9000/default")
    print(f"DB:       {engine.url}")
    print(f"Domain:   {DOMAIN}")

    heading("1. Reset + seed an acme.test tenant + draft campaign")
    await reset_demo_state()
    tenant_id, campaign_id = await seed_tenant_and_campaign()
    print(f"  tenant_id   = {tenant_id}")
    print(f"  campaign_id = {campaign_id}")

    spans_before = otel_span_count()
    print(f"  collector span count (before): {spans_before}")

    heading("2. Full OIDC handshake via oidc-mock (no browser)")
    async with httpx.AsyncClient(base_url=APP_BASE, follow_redirects=True, timeout=10.0) as client:
        login = await client.get("/api/auth/login")
        print(f"  /api/auth/login -> final {login.status_code} at {login.url.path}")
        if login.status_code != 200:
            print(f"  body: {login.text[:400]}")
            raise SystemExit("OIDC handshake failed")
        me_body = login.json()
        print(f"  /api/me payload: {json.dumps(me_body, indent=4)}")

        heading("3. RBAC: viewer hits marketer-only endpoint")
        forbidden = await client.get("/api/_protected/marketer")
        print(f"  /api/_protected/marketer (as viewer) -> {forbidden.status_code}")
        print(f"  detail: {forbidden.json().get('detail')}")
        assert forbidden.status_code == 403

        heading("4. Promote JIT user to marketer and retry")
        user = await fetch_app_user(me_body["email"])
        assert user is not None
        await promote_user_to_marketer(user.id)
        print(f"  promoted {user.email}: viewer -> marketer")

        ok = await client.get("/api/_protected/marketer")
        print(f"  /api/_protected/marketer (as marketer) -> {ok.status_code}")
        print(f"  body: {ok.json()}")
        assert ok.status_code == 200

        heading("5. POST /api/campaigns/{id}/transitions/echo_step")
        resp = await client.post(f"/api/campaigns/{campaign_id}/transitions/echo_step")
        print(f"  status: {resp.status_code}, body: {resp.json()}")
        assert resp.status_code == 200

    heading("6. Wait for the worker to consume the echo task")
    deadline = time.monotonic() + 15
    final = None
    while time.monotonic() < deadline:
        async with SessionLocal() as session:
            tasks = (
                (await session.execute(select(Task).where(Task.campaign_id == campaign_id)))
                .scalars()
                .all()
            )
            if tasks and all(t.status.value in {"succeeded", "failed"} for t in tasks):
                final = tasks[0]
                break
        await asyncio.sleep(0.5)
    if final is None:
        print("  WARNING: task did not finish within 15s")
    else:
        print(f"  task {final.id} -> status={final.status.value} attempt={final.attempt}")

    heading("7. Tasks for this campaign")
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Task).where(Task.campaign_id == campaign_id).order_by(Task.created_at)
                )
            )
            .scalars()
            .all()
        )
    for t in rows:
        print(
            f"  [{t.status.value:>9}] skill={t.skill_name:<10} "
            f"attempt={t.attempt} output={short(json.dumps(t.output_data))}"
        )

    heading("audit_log for this tenant")
    async with SessionLocal() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == tenant_id)
                    .order_by(AuditLog.logged_at)
                )
            )
            .scalars()
            .all()
        )
    for row in audits:
        before = short(json.dumps(row.before_state)) if row.before_state else "-"
        after = short(json.dumps(row.after_state)) if row.after_state else "-"
        print(
            f"  [{row.actor_kind:>6}] {row.entity_kind:<10} {row.action:<22}"
            f"  before={before}  after={after}"
        )

    heading("agent_log for this tenant")
    async with SessionLocal() as session:
        logs = (
            (
                await session.execute(
                    select(AgentLog)
                    .where(AgentLog.tenant_id == tenant_id)
                    .order_by(AgentLog.logged_at)
                )
            )
            .scalars()
            .all()
        )
    if not logs:
        print("  (none — echo handler doesn't emit; only tool-handler tasks do)")
    for log in logs:
        print(f"  [{log.severity:>5}] {log.action:<28} attempt={log.log_data.get('attempt')}")

    heading("OTel spans flushed during the demo")
    await asyncio.sleep(6)
    spans_after = otel_span_count()
    print(f"  collector span count (after): {spans_after}")
    print(f"  delta: {spans_after - spans_before} new spans")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.ConnectError as exc:
        print(f"\nERROR: could not reach {APP_BASE} — is the app running?", file=sys.stderr)
        print(f"  ({exc})", file=sys.stderr)
        sys.exit(1)

"""End-to-end Slice 1 demo.

Drives a campaign through the state machine and the worker, then dumps the
resulting `task`, `audit_log`, and `agent_log` rows so you can see every
Slice 1 layer (auth/RBAC isn't exercised because OIDC mock isn't wired into
docker-compose yet; we go directly through the app's Python surface).

Run:    .venv/bin/python -m scripts.slice1_demo
"""

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text, update

from app.audit import register_listeners
from app.db.enums import CampaignType
from app.db.models import AgentLog, AuditLog, Campaign, Task, Tenant
from app.db.session import SessionLocal, engine
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.queue import enqueue_task
from app.orchestrator.state_machine import campaign_sm
from app.orchestrator.worker import run_once
from app.tools import register_builtin_tools

DASH = "─" * 78


def heading(title: str) -> None:
    print(f"\n{DASH}\n{title}\n{DASH}")


def short(value: object, max_chars: int = 60) -> str:
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


async def clean_demo_state() -> None:
    """Wipe demo tenants and any task/agent_log references so re-runs are idempotent."""
    async with SessionLocal() as session, session.begin():
        await session.execute(text("DELETE FROM agent_log WHERE task_id IS NOT NULL"))
        await session.execute(
            text(
                "DELETE FROM task WHERE campaign_id IN (SELECT id FROM campaign WHERE name LIKE 'demo-%')"
            )
        )
        await session.execute(text("DELETE FROM campaign WHERE name LIKE 'demo-%'"))
        await session.execute(
            text(
                "DELETE FROM agent WHERE name = 'Marketing Orchestrator' AND tenant_id IN (SELECT id FROM tenant WHERE name LIKE 'demo-%')"
            )
        )
        await session.execute(
            text(
                "DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenant WHERE name LIKE 'demo-%')"
            )
        )
        await session.execute(text("DELETE FROM tenant WHERE name LIKE 'demo-%'"))


async def main() -> None:
    register_listeners()
    register_builtin_tools()
    register_builtin_handlers()

    heading("Slice 1 demo — environment")
    print(f"DB:     {engine.url}")
    print(f"Time:   {datetime.now(UTC).isoformat()}")

    await clean_demo_state()

    # ------------------------------------------------------------------ Step 1
    heading("Step 1 — create a tenant + drafted campaign")
    async with SessionLocal() as session, session.begin():
        tenant = Tenant(name=f"demo-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name=f"demo-spring-launch-{uuid.uuid4().hex[:4]}",
            campaign_type=CampaignType.product_launch,
            objective="Demo Slice 1 end-to-end",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
        )
        session.add(campaign)
        await session.flush()
        tenant_id, campaign_id = tenant.id, campaign.id
    print(f"  tenant_id   = {tenant_id}")
    print(f"  campaign_id = {campaign_id}")
    print("  audit listener fires on each insert; see audit_log dump at the end.")

    # ------------------------------------------------------------------ Step 2
    heading("Step 2 — apply state-machine transition `echo_step`")
    async with SessionLocal() as session, session.begin():
        cmp = await session.get(Campaign, campaign_id)
        assert cmp is not None
        print(f"  status before: {cmp.status.value}")
        await campaign_sm.apply(session, cmp, "echo_step")
        print(f"  status after:  {cmp.status.value} (self-loop placeholder)")
    print("  -> wrote audit_log action='transition_applied'")
    print("  -> enqueued a queue task with skill_name='echo'")

    # ------------------------------------------------------------------ Step 3
    heading("Step 3 — worker run_once consumes the echo task")
    session_maker = SessionLocal
    handled = await run_once(worker_id="demo-worker", session_maker=session_maker)
    print(f"  worker handled a task: {handled}")

    # ------------------------------------------------------------------ Step 4
    heading("Step 4 — enqueue an `echo_tool` task (tool layer + agent_log)")
    async with SessionLocal() as session, session.begin():
        # Need an Agent row for the tool task; the state machine already
        # created one in step 2. Reuse it.
        agent_id_row = (
            await session.execute(
                text(
                    "SELECT id FROM agent WHERE tenant_id = :tid "
                    "AND name = 'Marketing Orchestrator' LIMIT 1"
                ),
                {"tid": str(tenant_id)},
            )
        ).scalar_one()
        await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id_row,
            campaign_id=campaign_id,
            skill_name="echo_tool",
            input_data={"greeting": "hello from echo_tool"},
        )
    await run_once(worker_id="demo-worker", session_maker=session_maker)
    print("  -> tool handler wrapped the call; emitted tool.echo_tool.succeeded log")

    # ------------------------------------------------------------------ Step 5
    heading("Step 5 — enqueue `flaky_tool` (fails twice, succeeds on attempt 3)")
    async with SessionLocal() as session, session.begin():
        flaky_task = await enqueue_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id_row,
            campaign_id=campaign_id,
            skill_name="flaky_tool",
            input_data={"fail_count": 2},
            max_attempts=5,
        )
        flaky_task_id = flaky_task.id

    for cycle in (1, 2, 3):
        await run_once(worker_id="demo-worker", session_maker=session_maker)
        # The orchestrator's backoff pushes scheduled_for into the future; for a
        # tight demo we yank it back to now() so the worker can re-claim.
        async with SessionLocal() as session, session.begin():
            await session.execute(
                update(Task).where(Task.id == flaky_task_id).values(scheduled_for=datetime.now(UTC))
            )
        async with SessionLocal() as session:
            t = await session.get(Task, flaky_task_id)
            assert t is not None
            print(f"  cycle {cycle}: attempt={t.attempt}, status={t.status.value}")

    # ------------------------------------------------------------------ Summary
    heading("Tasks for this demo")
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
            f"  [{t.status.value:>9}] skill={t.skill_name:<11} attempt={t.attempt} "
            f"output={short(json.dumps(t.output_data))}"
        )

    heading("audit_log for this demo (campaign + tenant + transitions)")
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
            f"  [{row.actor_kind:>6}] {row.entity_kind:<10} {row.action:<22} "
            f"before={before} after={after}"
        )

    heading("agent_log for this demo")
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
    for log in logs:
        latency = log.log_data.get("latency_ms")
        attempt = log.log_data.get("attempt")
        err = log.log_data.get("error")
        line = f"  [{log.severity:>5}] {log.action:<28} attempt={attempt} latency={latency}ms"
        if err:
            line += f"  err={short(err, 40)}"
        print(line)

    heading("Done")
    print("HTTP requests to the running app produce OTel spans visible via")
    print("  docker logs mas-otel | grep -i 'Span #'")
    print("The OIDC mock is at http://localhost:9000/default for full-handshake demos.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

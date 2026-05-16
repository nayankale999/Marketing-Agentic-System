"""W7: campaign state machine + orchestrator + queue end-to-end.

Drives the Slice-1 placeholder `echo_step: drafted -> drafted` through 5
round trips. Each apply must:
  - keep the campaign in `drafted` (self-loop)
  - enqueue exactly one echo task
  - write an audit_log row with action='transition_applied'
And a separate test confirms the worker consumes the enqueued task end-to-end.
"""

import uuid
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import CampaignStatus, CampaignType, TaskStatus, UserRole
from app.db.models import AppUser, AuditLog, Campaign, Task, Tenant
from app.orchestrator.handlers import register_builtin_handlers
from app.orchestrator.state_machine import (
    UnknownTransitionError,
    campaign_sm,
)
from app.orchestrator.worker import run_once

register_builtin_handlers()


@pytest.fixture(autouse=True)
async def _clean_task_queue(db_engine: AsyncEngine):
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        await session.execute(text("DELETE FROM task"))
    yield


@pytest.fixture
async def campaign_in_drafted(db_engine: AsyncEngine):
    """Create a tenant + a drafted campaign; return (tenant_id, campaign_id)."""
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        tenant = Tenant(name=f"sm-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name=f"camp-{uuid.uuid4().hex[:8]}",
            campaign_type=CampaignType.awareness,
            objective="test",
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
        )
        session.add(campaign)
        await session.commit()
        return tenant.id, campaign.id


async def test_echo_step_self_loop(db_engine: AsyncEngine, campaign_in_drafted) -> None:
    tenant_id, campaign_id = campaign_in_drafted

    # Apply 5 times, each in its own tx.
    for _ in range(5):
        async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
            campaign = await session.get(Campaign, campaign_id)
            assert campaign is not None
            assert campaign.status == CampaignStatus.drafted
            await campaign_sm.apply(session, campaign, "echo_step")
            assert campaign.status == CampaignStatus.drafted

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        # Exactly 5 echo tasks enqueued for this campaign.
        tasks = (
            (await session.execute(select(Task).where(Task.campaign_id == campaign_id)))
            .scalars()
            .all()
        )
        assert len(tasks) == 5
        assert all(t.skill_name == "echo" for t in tasks)
        assert all(t.status == TaskStatus.queued for t in tasks)
        assert all(t.tenant_id == tenant_id for t in tasks)

        # Exactly 5 transition_applied audit_log rows.
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == campaign_id,
                        AuditLog.action == "transition_applied",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 5
        for row in audits:
            assert row.before_state == {"status": "drafted"}
            assert row.after_state == {"status": "drafted"}
            assert row.extra_metadata == {"transition": "echo_step"}


async def test_unknown_transition_raises(db_engine: AsyncEngine, campaign_in_drafted) -> None:
    _, campaign_id = campaign_in_drafted
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        with pytest.raises(UnknownTransitionError, match="no transition 'does_not_exist'"):
            await campaign_sm.apply(session, campaign, "does_not_exist")


async def test_worker_consumes_enqueued_echo_task(
    db_engine: AsyncEngine, campaign_in_drafted
) -> None:
    _, campaign_id = campaign_in_drafted

    # Drive one transition; one echo task should be enqueued.
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        await campaign_sm.apply(session, campaign, "echo_step")

    # Run the worker once. It should claim and complete the echo task.
    session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    handled = await run_once(worker_id="sm-test-worker", session_maker=session_maker)
    assert handled is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task = (
            await session.execute(select(Task).where(Task.campaign_id == campaign_id))
        ).scalar_one()
        assert task.status == TaskStatus.succeeded
        assert task.output_data == {
            "campaign_id": str(campaign_id),
            "transition": "echo_step",
        }


async def test_api_transition_endpoint_drives_state_machine(
    db_engine: AsyncEngine, campaign_in_drafted, override_api_db
) -> None:
    """POST /api/campaigns/{id}/transitions/echo_step works end-to-end with auth."""
    tenant_id, campaign_id = campaign_in_drafted

    # Build a fake marketer in the campaign's tenant and override the dep.
    user = AppUser(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=f"marketer-{uuid.uuid4().hex[:8]}@sm.test",
        role=UserRole.marketer,
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: user

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/api/campaigns/{campaign_id}/transitions/echo_step")
            assert response.status_code == 200
            assert response.json() == {
                "id": str(campaign_id),
                "status": "drafted",
            }

            # Bad transition -> 409 with allowed-set in the message.
            bad = await client.post(f"/api/campaigns/{campaign_id}/transitions/no_such_transition")
            assert bad.status_code == 409
            assert "no transition 'no_such_transition'" in bad.json()["detail"]

            # Bogus campaign id -> 404.
            missing = await client.post(f"/api/campaigns/{uuid.uuid4()}/transitions/echo_step")
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # Confirm the API-driven transition wrote its audit + enqueued its task.
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        task_count = (
            (await session.execute(select(Task).where(Task.campaign_id == campaign_id)))
            .scalars()
            .all()
        )
        assert len(task_count) == 1

        audit_count = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == campaign_id,
                        AuditLog.action == "transition_applied",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_count) == 1


async def test_transitions_from_drafted_lists_echo_step() -> None:
    """Public-API check on the state machine's introspection."""
    assert "echo_step" in campaign_sm.transitions_from(CampaignStatus.drafted)

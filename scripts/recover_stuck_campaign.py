"""Drive a single drafted campaign through to `ready_to_launch`.

Walks the same tool path the dashboard assistant would: synthesise
audience → run Strategist (live Anthropic) → accept proposal → run
Content Creator (live Anthropic, one call per touchpoint) → approve
all drafts. After this, you click `Launch` in the browser.

Usage:
    .venv/bin/python -m scripts.recover_stuck_campaign <campaign-id-or-name>
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.assistant.tools import (
    accept_strategy,
    approve_all_content,
    generate_content,
    generate_strategy,
    synthesise_audience,
    _find_campaign,
)
from app.db.enums import CampaignStatus, UserRole
from app.db.models import AppUser
from app.db.session import SessionLocal, set_tenant_context


DASH = "─" * 78


def heading(t: str) -> None:
    print(f"\n{DASH}\n  {t}\n{DASH}")


def step(label: str, body=None) -> None:
    print(f"\n• {label}")
    if body is None:
        return
    if isinstance(body, str):
        for line in body.splitlines():
            print(f"    {line}")
    else:
        import json

        print(
            "    " + json.dumps(body, indent=2, default=str).replace("\n", "\n    ")
        )


async def _pick_user(session, tenant_id) -> AppUser:
    """Find a manager/admin in the tenant. Falls back to the first user."""
    row = (
        await session.execute(
            select(AppUser)
            .where(
                AppUser.tenant_id == tenant_id,
                AppUser.role.in_([UserRole.admin, UserRole.manager]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    return (
        await session.execute(
            select(AppUser).where(AppUser.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one()


async def run(identifier: str) -> int:
    # First lookup: find the campaign so we can resolve its tenant.
    async with SessionLocal() as session:
        from app.db.models import Campaign

        # Direct lookup against the DB (no RLS at this point — we haven't
        # set tenant context yet).
        try:
            from uuid import UUID

            cid = UUID(identifier)
            c = await session.get(Campaign, cid)
        except (ValueError, TypeError):
            c = (
                await session.execute(
                    select(Campaign).where(Campaign.name.ilike(f"%{identifier}%"))
                )
            ).scalars().first()
        if c is None:
            print(f"ERROR: no campaign matches '{identifier}'.")
            return 2
        tenant_id = c.tenant_id
        campaign_id = c.id
        campaign_name = c.name

    heading(f"Recovering: {campaign_name}")

    # Now switch to tenant context for all subsequent operations.
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        user = await _pick_user(session, tenant_id)
        c = await session.get(Campaign, campaign_id)
        step("Loaded", {
            "id": str(c.id),
            "name": c.name,
            "status": c.status.value,
            "owner_role": user.role.value,
        })

    # 1. Synthesise audience (skip if already has one).
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        c = await session.get(Campaign, campaign_id)
        if c.status == CampaignStatus.drafted:
            heading("1. Build synthetic audience")
            result = await synthesise_audience(
                session,
                user=user,
                campaign=str(campaign_id),
                size=20,
                persona="Mid-market non-financial-services compliance buyers",
            )
            step(result.summary, result.data)

    # 2. Generate strategy.
    heading("2. Run Strategist (LIVE Anthropic call)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        result = await generate_strategy(
            session, user=user, campaign=str(campaign_id), confirm=True
        )
        step(result.summary, {
            "version": result.data.get("version"),
            "proposal_id": result.data.get("proposal_id"),
            "channels": [
                {"name": ch.get("name"), "pct": ch.get("allocation_pct")}
                for ch in (result.data.get("proposal_payload") or {}).get("channels", [])
            ],
        })

    # 3. Accept strategy.
    heading("3. Accept the proposal")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        result = await accept_strategy(
            session, user=user, campaign=str(campaign_id), confirm=True
        )
        step(result.summary, result.data)

    # 4. Generate content.
    heading("4. Draft content (LIVE Anthropic call per touchpoint)")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        result = await generate_content(
            session, user=user, campaign=str(campaign_id), confirm=True
        )
        step(result.summary, result.data)

    # 5. Approve all drafts (need manager+).
    heading("5. Approve all drafts")
    async with SessionLocal() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        # Pick a manager+ user explicitly.
        manager = await _pick_user(session, tenant_id)
        if manager.role not in (UserRole.manager, UserRole.admin):
            print(
                "    NOTE: no manager/admin user in this tenant; using "
                f"{manager.email} ({manager.role.value})"
            )
        result = await approve_all_content(
            session, user=manager, campaign=str(campaign_id), confirm=True
        )
        step(result.summary, result.data)

    # Final report.
    heading("Done — what to do next")
    async with SessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        c = await session.get(Campaign, campaign_id)
        step("Final campaign state", {
            "name": c.name,
            "status": c.status.value,
            "url": f"http://localhost:8001/ui/campaigns/{c.id}",
        })
        print(
            "\n  Open the URL above to see the campaign detail, the\n"
            "  generated strategy proposal, the drafted content, and a\n"
            "  Launch button (via the assistant)."
        )
        print(
            "\n  Or in the assistant, type:\n"
            f"    Launch {c.name}\n"
        )
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: recover_stuck_campaign.py <campaign-id-or-name>", file=sys.stderr)
        return 2
    return asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())

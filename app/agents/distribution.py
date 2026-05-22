"""Channel Distribution agent (W28, E08-S01/S02/S05).

Two responsibilities:

  * `schedule_approved_assets` — called from `start_launch.on_enter`. Walks
    every approved asset, pulls its slot from the touchpoint calendar (W21),
    flips the row to `scheduled`, and enqueues a `distribution.dispatch_email`
    task gated to that slot via `scheduled_for`.

  * `dispatch_email_asset` — called by the queue handler when a scheduled
    asset's slot arrives. Walks the audience, dedup-filters via
    `dispatch_attempt`, calls the W27 EmailDispatchTool, persists one
    attempt row per recipient, flips the asset to `published`, and
    `_maybe_go_live`s the campaign.

On any unrecoverable failure (no provider configured, no audience, bad
asset state) the agent raises a typed error so the queue's retry path
can record it cleanly in agent_log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.context import current_actor_id, current_actor_kind
from app.audit.writer import column_snapshot, write_audit
from app.db.enums import (
    AgentKind,
    AssetStatus,
    AssetType,
    CampaignStatus,
)
from app.db.models import (
    Agent,
    Audience,
    AudienceMember,
    Campaign,
    ContentAsset,
    DispatchAttempt,
    IntegrationCredential,
    StrategyProposal,
    StrategyTouchpoint,
)
from app.integrations.credentials import get_encrypted_payload
from app.integrations.email import (
    EmailConnector,
    ProviderUnreachableError,
    build_email_connector,
)
from app.orchestrator.queue import enqueue_task
from app.tools.email_dispatch import (
    DispatchValidationError,
    EmailDispatchTool,
)


_DEDUP_WINDOW = timedelta(hours=24)


class DistributionPreconditionError(Exception):
    """Raised when scheduling or dispatch can't proceed — caller maps to a
    422 at the API layer, or a queue handler failure at the worker layer."""


# ---------------------------------------------------------------------------
# Agent bootstrap
# ---------------------------------------------------------------------------


async def ensure_distribution_agent(session: AsyncSession, tenant_id: UUID) -> Agent:
    existing = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == AgentKind.channel_distribution,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    agent = Agent(
        tenant_id=tenant_id,
        name="Channel Distribution",
        agent_type=AgentKind.channel_distribution,
    )
    session.add(agent)
    await session.flush()
    return agent


# ---------------------------------------------------------------------------
# Scheduling (E08-S01)
# ---------------------------------------------------------------------------


async def schedule_approved_assets(
    session: AsyncSession, *, campaign: Campaign
) -> list[ContentAsset]:
    """on_enter for `start_launch` — every approved required asset gets its
    `scheduled_at` populated from the touchpoint calendar and a dispatch task
    enqueued for that slot. Returns the rows that were scheduled."""
    assets = (
        await session.execute(
            select(ContentAsset).where(
                ContentAsset.campaign_id == campaign.id,
                ContentAsset.status == AssetStatus.approved,
            )
        )
    ).scalars().all()

    if not assets:
        return []

    # Resolve every referenced touchpoint in one query.
    touchpoint_ids = _collect_touchpoint_ids(assets)
    touchpoints_by_id = {}
    if touchpoint_ids:
        rows = (
            await session.execute(
                select(StrategyTouchpoint).where(StrategyTouchpoint.id.in_(touchpoint_ids))
            )
        ).scalars().all()
        touchpoints_by_id = {row.id: row for row in rows}

    agent = await ensure_distribution_agent(session, campaign.tenant_id)
    now = datetime.now(UTC)
    scheduled: list[ContentAsset] = []

    for asset in assets:
        before = column_snapshot(asset)
        tp_id = _touchpoint_id_for(asset)
        touchpoint = touchpoints_by_id.get(tp_id) if tp_id else None
        prior_slot = asset.scheduled_at

        if touchpoint is not None and touchpoint.scheduled_at >= now:
            asset.scheduled_at = touchpoint.scheduled_at
        elif touchpoint is not None and touchpoint.scheduled_at < now:
            # E08-S01 #3 backend default: send-now and log the warning so the
            # UI (W32) can surface "this slot was past, we picked now".
            asset.scheduled_at = now
            asset.extra_metadata = {
                **(asset.extra_metadata or {}),
                "schedule_warning": {
                    "kind": "past_slot",
                    "original_slot": touchpoint.scheduled_at.isoformat(),
                    "resolved_at": now.isoformat(),
                },
            }
        else:
            # No touchpoint — defensive fallback: send-now.
            asset.scheduled_at = asset.scheduled_at or now
            asset.extra_metadata = {
                **(asset.extra_metadata or {}),
                "schedule_warning": {
                    "kind": "no_touchpoint",
                    "resolved_at": now.isoformat(),
                },
            }

        asset.status = AssetStatus.scheduled
        await session.flush()

        write_audit(
            session,
            tenant_id=asset.tenant_id,
            actor_kind=current_actor_kind.get(),
            actor_id=current_actor_id.get(),
            entity_kind="content_asset",
            entity_id=asset.id,
            action="scheduled",
            before_state=before,
            after_state=column_snapshot(asset),
            metadata={
                "prior_slot": prior_slot.isoformat() if prior_slot else None,
                "new_slot": asset.scheduled_at.isoformat(),
                "touchpoint_id": str(tp_id) if tp_id else None,
            },
        )

        await enqueue_task(
            session,
            tenant_id=asset.tenant_id,
            agent_id=agent.id,
            campaign_id=campaign.id,
            skill_name="distribution.dispatch_email",
            input_data={
                "asset_id": str(asset.id),
                "campaign_id": str(campaign.id),
            },
            scheduled_for=asset.scheduled_at,
        )
        scheduled.append(asset)

    return scheduled


# ---------------------------------------------------------------------------
# Email dispatch (E08-S02 + E08-S05)
# ---------------------------------------------------------------------------


async def dispatch_email_asset(
    session: AsyncSession, *, asset_id: UUID
) -> dict[str, Any]:
    """Run one `distribution.dispatch_email` task. Loads the asset + its
    audience, dedup-filters via dispatch_attempt, calls the email tool,
    writes per-recipient attempt rows, flips the asset to `published`,
    and drives the campaign to `live` when conditions are met.

    Raises `DistributionPreconditionError` for retryable-by-fixing-config
    issues (no email integration, no audience). Provider transport failures
    propagate as `ProviderUnreachableError` so the queue's retry path picks
    them up without flipping the asset off `scheduled`.
    """
    asset = await session.get(ContentAsset, asset_id)
    if asset is None:
        raise DistributionPreconditionError(f"content_asset {asset_id} not found")
    if asset.status != AssetStatus.scheduled:
        raise DistributionPreconditionError(
            f"asset {asset_id} is in '{asset.status.value}', not eligible for dispatch"
        )

    campaign = await session.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise DistributionPreconditionError(
            f"campaign {asset.campaign_id} not found"
        )
    if asset.asset_type != AssetType.email:
        raise DistributionPreconditionError(
            f"asset {asset_id} is type '{asset.asset_type.value}'; email dispatcher only handles 'email'"
        )

    connector, payload = await _load_email_connector(session, tenant_id=asset.tenant_id)
    audience_members = await _load_audience_members(session, campaign_id=asset.campaign_id)
    if not audience_members:
        raise DistributionPreconditionError(
            "no audience members materialised for this campaign"
        )

    # Skip recipients that already have a sent attempt for this asset (the
    # primary idempotency check, E08-S05 #1) OR a sent attempt to the same
    # address within the last 24h (the safety-net for E08-S05 #3).
    existing_attempts_by_recipient = await _existing_attempts(
        session, content_asset_id=asset.id
    )
    recent_sends_by_recipient = await _recent_sent_recipients(
        session,
        tenant_id=asset.tenant_id,
        recipients=[m.payload.get("email", "") for m in audience_members if m.payload],
    )

    to_send: list[tuple[AudienceMember, str]] = []
    deduped: list[tuple[AudienceMember, str]] = []
    for member in audience_members:
        email = str((member.payload or {}).get("email", "")).strip().lower()
        if not email:
            continue
        if email in existing_attempts_by_recipient:
            deduped.append((member, email))
            continue
        if email in recent_sends_by_recipient:
            deduped.append((member, email))
            continue
        to_send.append((member, email))

    from_email = payload.get("default_from_email") or ""
    tool = EmailDispatchTool(
        connector=connector, session=session, tenant_id=asset.tenant_id
    )

    sent_count = 0
    suppressed_count = 0
    rejected_count = 0

    if to_send:
        message = _message_from_asset(asset)
        audience_batch = [
            {
                "email": email,
                "merge_fields": _string_only_payload(member.payload or {}),
            }
            for member, email in to_send
        ]
        result = await tool.call(
            {
                "from_email": from_email,
                "audience_batch": audience_batch,
                "message": message,
                "idempotency_key": f"asset:{asset.id}",
            }
        )

        accepted_map: dict[str, str | None] = {
            entry["email"].lower(): entry.get("provider_message_id")
            for entry in result.get("per_message_ids", [])
        }
        rejection_map: dict[str, str] = {
            entry["email"].lower(): entry.get("reason", "rejected")
            for entry in result.get("rejections", [])
        }

        now = datetime.now(UTC)
        for member, email in to_send:
            idem_key = _idempotency_key(asset.id, email)
            if email in accepted_map:
                status = "sent"
                sent_count += 1
                provider_message_id = accepted_map[email]
                last_error = None
                sent_at: datetime | None = now
            elif email in rejection_map:
                reason = rejection_map[email]
                status = "suppressed" if reason == "suppressed" else "rejected"
                if status == "suppressed":
                    suppressed_count += 1
                else:
                    rejected_count += 1
                provider_message_id = None
                last_error = reason
                sent_at = None
            else:
                # Provider accepted the call but didn't acknowledge this
                # recipient — record as `failed` so the operator notices.
                status = "failed"
                rejected_count += 1
                provider_message_id = None
                last_error = "no_provider_acknowledgement"
                sent_at = None

            await _upsert_dispatch_attempt(
                session,
                tenant_id=asset.tenant_id,
                content_asset_id=asset.id,
                audience_external_id=member.external_id,
                recipient_identifier=email,
                idempotency_key=idem_key,
                provider=connector.provider,
                provider_message_id=provider_message_id,
                status=status,
                last_error=last_error,
                sent_at=sent_at,
            )

    # Even if nothing new was sent, the dedup count is meaningful — capture it.
    summary = {
        "sent": sent_count,
        "suppressed": suppressed_count,
        "rejected": rejected_count,
        "deduped": len(deduped),
        "audience_size": len(audience_members),
    }
    before = column_snapshot(asset)
    asset.status = AssetStatus.published
    asset.published_at = datetime.now(UTC)
    asset.extra_metadata = {
        **(asset.extra_metadata or {}),
        "dispatch_summary": summary,
        "dispatch_provider": connector.provider,
    }
    await session.flush()

    write_audit(
        session,
        tenant_id=asset.tenant_id,
        actor_kind=current_actor_kind.get(),
        actor_id=current_actor_id.get(),
        entity_kind="content_asset",
        entity_id=asset.id,
        action="published",
        before_state=before,
        after_state=column_snapshot(asset),
        metadata={"dispatch_summary": summary},
    )

    await _maybe_go_live(session, campaign=campaign)

    return {
        "asset_id": str(asset.id),
        "status": asset.status.value,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# State transition: ready_to_launch → live
# ---------------------------------------------------------------------------


async def _maybe_go_live(session: AsyncSession, *, campaign: Campaign) -> None:
    """If we're ready and start_date has arrived, advance to `live`. Called
    after every successful dispatch — idempotent once the campaign moved on."""
    if campaign.status != CampaignStatus.ready_to_launch:
        return
    from app.orchestrator.state_machine import (
        GuardFailedError,
        UnknownTransitionError,
        campaign_sm,
    )

    try:
        await campaign_sm.apply(session, campaign, "start_live")
    except (UnknownTransitionError, GuardFailedError):
        return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _idempotency_key(content_asset_id: UUID, recipient_email: str) -> str:
    return f"asset:{content_asset_id}:recipient:{recipient_email.lower()}"


def _touchpoint_id_for(asset: ContentAsset) -> UUID | None:
    raw = (asset.extra_metadata or {}).get("touchpoint_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


def _collect_touchpoint_ids(assets: list[ContentAsset]) -> list[UUID]:
    out: list[UUID] = []
    for asset in assets:
        tp_id = _touchpoint_id_for(asset)
        if tp_id is not None:
            out.append(tp_id)
    return out


def _message_from_asset(asset: ContentAsset) -> dict[str, Any]:
    fields = (asset.extra_metadata or {}).get("fields") or {}
    body = asset.content or ""
    return {
        "subject": str(fields.get("subject") or asset.title or "(no subject)"),
        "text_body": body,
        "html_body": body,  # MVP: same content; richer rendering later
        "headers": {},
    }


def _string_only_payload(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in payload.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[str(k)] = str(v)
    return out


async def _load_email_connector(
    session: AsyncSession, *, tenant_id: UUID
) -> tuple[EmailConnector, dict[str, Any]]:
    cred = (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.tenant_id == tenant_id,
                IntegrationCredential.provider == "sendgrid",
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        raise DistributionPreconditionError(
            "no email integration configured for this tenant"
        )
    payload = get_encrypted_payload().decrypt(cred.encrypted_payload)
    connector = build_email_connector(cred.provider, payload=payload)
    return connector, payload


async def _load_audience_members(
    session: AsyncSession, *, campaign_id: UUID
) -> list[AudienceMember]:
    audience = (
        await session.execute(
            select(Audience)
            .where(Audience.campaign_id == campaign_id)
            .order_by(Audience.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if audience is None:
        return []
    return (
        await session.execute(
            select(AudienceMember).where(AudienceMember.audience_id == audience.id)
        )
    ).scalars().all()


async def _existing_attempts(
    session: AsyncSession, *, content_asset_id: UUID
) -> dict[str, DispatchAttempt]:
    """Return a `recipient_email_lowercase → DispatchAttempt` map for every
    prior attempt against this asset. Used to skip already-sent recipients
    on retry (E08-S05 #1/#2)."""
    rows = (
        await session.execute(
            select(DispatchAttempt).where(
                DispatchAttempt.content_asset_id == content_asset_id,
                DispatchAttempt.status == "sent",
            )
        )
    ).scalars().all()
    return {row.recipient_identifier.lower(): row for row in rows}


async def _recent_sent_recipients(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    recipients: list[str],
) -> set[str]:
    """E08-S05 #3 safety net: any recipient with a `sent` attempt in the
    last 24h gets skipped, regardless of asset. Catches the
    "sent-but-DB-write-crashed" scenario where idempotency_key wouldn't
    match because we never wrote it."""
    normalised = {r.strip().lower() for r in recipients if r}
    if not normalised:
        return set()
    cutoff = datetime.now(UTC) - _DEDUP_WINDOW
    rows = (
        await session.execute(
            select(DispatchAttempt.recipient_identifier).where(
                DispatchAttempt.tenant_id == tenant_id,
                DispatchAttempt.status == "sent",
                DispatchAttempt.sent_at >= cutoff,
                DispatchAttempt.recipient_identifier.in_(normalised),
            )
        )
    ).scalars().all()
    return {str(r).lower() for r in rows}


async def _upsert_dispatch_attempt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    content_asset_id: UUID,
    audience_external_id: str | None,
    recipient_identifier: str,
    idempotency_key: str,
    provider: str,
    provider_message_id: str | None,
    status: str,
    last_error: str | None,
    sent_at: datetime | None,
) -> None:
    """Upsert per (tenant_id, idempotency_key). On conflict we bump the
    attempt counter and keep the existing terminal status — never overwrite
    a `sent` row with a worse outcome on retry."""
    stmt = (
        pg_insert(DispatchAttempt)
        .values(
            tenant_id=tenant_id,
            content_asset_id=content_asset_id,
            audience_external_id=audience_external_id,
            recipient_identifier=recipient_identifier,
            idempotency_key=idempotency_key,
            provider=provider,
            provider_message_id=provider_message_id,
            status=status,
            last_error=last_error,
            sent_at=sent_at,
        )
        .on_conflict_do_update(
            index_elements=[
                DispatchAttempt.tenant_id,
                DispatchAttempt.idempotency_key,
            ],
            set_={
                "attempt_count": DispatchAttempt.attempt_count + 1,
                "last_error": last_error,
                "updated_at": func.now(),
            },
            where=DispatchAttempt.status != "sent",
        )
    )
    await session.execute(stmt)


__all__ = [
    "DistributionPreconditionError",
    "dispatch_email_asset",
    "ensure_distribution_agent",
    "schedule_approved_assets",
]

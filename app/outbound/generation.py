"""Per-segment LinkedIn DM + email generation (W43).

Given a Campaign + an Audience that's been segmented, generate ONE
LinkedIn DM template + ONE email template per non-empty segment.
Each template uses merge tokens (`{first_name}`, `{company}`, `{title}`)
that the rendering layer fills in per-contact at view time.

We persist the templates as ContentAsset rows tagged with:
  * asset_type = email | linkedin_dm
  * extra_metadata.segment_key — which seniority bucket
  * extra_metadata.audience_id — what the SDR uploaded
  * extra_metadata.merge_tokens — list of tokens used in the body

This way the existing approval workflow + audit trail continue to
work; the only new behaviour is the per-contact render at view time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AssetStatus, AssetType
from app.db.models import Audience, AudienceMember, Campaign, ContentAsset
from app.outbound.segmentation import (
    SegmentationResult,
    segment_members,
)


# Tokens we instruct the model to use. Listed in the prompt so the
# model knows to leave them as literal `{first_name}` etc. — not
# fill them with sample data.
_MERGE_TOKENS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "company",
    "title",
)

_LINKEDIN_BUDGET_CHARS = 800   # LinkedIn caps connection-request DMs at 1k
_EMAIL_SUBJECT_BUDGET = 90     # most inboxes truncate around 90
_EMAIL_BODY_BUDGET_CHARS = 1500  # ~250 words; keeps it skim-able


class OutreachGenerationError(Exception):
    """Wrap any error from the LLM call so the caller can fall back."""


@dataclass(frozen=True)
class OutreachGenerationSummary:
    campaign_id: UUID
    audience_id: UUID
    segments_generated: int
    linkedin_assets: int
    email_assets: int
    skipped_existing: int
    errors: list[str] = field(default_factory=list)


def _build_segment_prompt(
    *,
    campaign: Campaign,
    segment: SegmentationResult,
    sample_members: list[AudienceMember],
    channel: str,
) -> str:
    """Compose the segment-aware instructions for one (segment, channel)
    pair. We give the model a tight brief + the segment's talking-point
    angle + 1-3 anonymised sample contacts so it can ground its hook in
    a real persona without learning specific PII."""
    sample_titles = sorted(
        {(m.payload or {}).get("title", "(unknown)") for m in sample_members[:3]}
    )
    sample_companies = sorted(
        {(m.payload or {}).get("company", "(unknown)") for m in sample_members[:3]}
    )

    token_list = ", ".join("{" + t + "}" for t in _MERGE_TOKENS)

    if channel == "linkedin_dm":
        channel_brief = (
            f"Write a LinkedIn connection-request DM (under "
            f"{_LINKEDIN_BUDGET_CHARS} characters). One paragraph. No "
            "subject line. Start with 'Hi {first_name},' on its own line."
        )
        required_json = '{"body": "..."}'
    else:  # email
        channel_brief = (
            f"Write a cold outbound email. Subject line under "
            f"{_EMAIL_SUBJECT_BUDGET} chars; body under "
            f"{_EMAIL_BODY_BUDGET_CHARS} chars. Body uses 2-3 short "
            "paragraphs + a one-line CTA. Start the body with "
            "'Hi {first_name},' on its own line."
        )
        required_json = '{"subject": "...", "body": "..."}'

    return (
        f"You are writing 1:1 outbound copy for the campaign "
        f"\"{campaign.name}\".\n"
        f"Campaign objective: {campaign.objective}\n"
        f"Campaign brief: {campaign.brief}\n\n"
        f"This template will be sent to {len(segment.members)} recipients in "
        f"the **{segment.bucket.label}** segment.\n"
        f"Sample titles in this segment: {', '.join(sample_titles) or 'n/a'}\n"
        f"Sample companies: {', '.join(sample_companies) or 'n/a'}\n\n"
        f"Tone guidance for this segment: {segment.bucket.talking_points}\n\n"
        f"{channel_brief}\n\n"
        f"MERGE TOKENS: the body must contain these literal tokens where "
        f"per-recipient values go: {token_list}. Use {{first_name}} at the "
        "opener. Use {{company}} or {{title}} at least once where it makes "
        "the message feel addressed to them. Do NOT invent placeholders; "
        "stick to the four listed.\n\n"
        f"Respond with a SINGLE JSON object: {required_json}. No prose, no "
        "markdown, no code fences."
    )


def _extract_json(raw: str) -> dict[str, str]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise OutreachGenerationError("model output was not a JSON object")
    return {k: v for k, v in obj.items() if isinstance(v, str)}


def _used_tokens(text: str) -> list[str]:
    return sorted({m for m in re.findall(r"\{([a-z_]+)\}", text) if m in _MERGE_TOKENS})


async def _generate_one(
    client: AsyncAnthropic,
    *,
    model: str,
    prompt: str,
) -> dict[str, str]:
    try:
        message: Message = await client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise OutreachGenerationError(f"LLM call failed: {exc}") from exc
    for block in message.content:
        if isinstance(block, TextBlock):
            return _extract_json(block.text)
    raise OutreachGenerationError("model returned no text content")


async def generate_outreach_drafts(
    session: AsyncSession,
    *,
    campaign: Campaign,
    audience_id: UUID,
    anthropic_client: AsyncAnthropic,
    model: str,
    channels: tuple[str, ...] = ("linkedin_dm", "email"),
    overwrite: bool = False,
) -> OutreachGenerationSummary:
    """Segment the audience, then for each (segment, channel) generate
    one template ContentAsset. If a template already exists for that
    (campaign, segment, channel) and `overwrite=False`, skip — caller
    can regenerate by passing overwrite=True."""

    audience = await session.get(Audience, audience_id)
    if audience is None or audience.tenant_id != campaign.tenant_id:
        raise OutreachGenerationError(
            f"audience {audience_id} not found in this tenant"
        )

    members = (
        await session.execute(
            select(AudienceMember).where(AudienceMember.audience_id == audience_id)
        )
    ).scalars().all()
    if not members:
        raise OutreachGenerationError("audience is empty")

    segments = segment_members(list(members))
    summary = OutreachGenerationSummary(
        campaign_id=campaign.id,
        audience_id=audience_id,
        segments_generated=len(segments),
        linkedin_assets=0,
        email_assets=0,
        skipped_existing=0,
    )

    for segment in segments:
        for channel in channels:
            asset_type = (
                AssetType.linkedin_dm if channel == "linkedin_dm" else AssetType.email
            )
            existing = (
                await session.execute(
                    select(ContentAsset).where(
                        ContentAsset.campaign_id == campaign.id,
                        ContentAsset.asset_type == asset_type,
                        ContentAsset.extra_metadata["segment_key"].astext
                        == segment.bucket.key,
                        ContentAsset.extra_metadata["audience_id"].astext
                        == str(audience_id),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and not overwrite:
                summary = _bump(summary, skipped_existing=1)
                continue

            prompt = _build_segment_prompt(
                campaign=campaign,
                segment=segment,
                sample_members=segment.members,
                channel=channel,
            )
            try:
                obj = await _generate_one(
                    anthropic_client, model=model, prompt=prompt
                )
            except OutreachGenerationError as exc:
                summary = _bump(summary, error=str(exc))
                continue

            fields: dict[str, Any]
            if channel == "linkedin_dm":
                body = obj.get("body") or ""
                if not body:
                    summary = _bump(summary, error="model returned empty linkedin body")
                    continue
                content_text = body
                fields = {"body": body}
            else:
                subject = obj.get("subject") or ""
                body = obj.get("body") or ""
                if not subject or not body:
                    summary = _bump(summary, error="model returned empty email fields")
                    continue
                content_text = f"{subject}\n\n{body}"
                fields = {"subject": subject, "body": body}

            title = f"{segment.bucket.label} · {channel}"
            metadata: dict[str, Any] = {
                "segment_key": segment.bucket.key,
                "segment_label": segment.bucket.label,
                "audience_id": str(audience_id),
                "recipient_count": len(segment.members),
                "merge_tokens": _used_tokens(content_text),
                "fields": fields,
                "personalisation_kind": "outbound_segment_template",
            }
            if existing is not None and overwrite:
                existing.content = content_text
                existing.extra_metadata = {
                    **(existing.extra_metadata or {}),
                    **metadata,
                }
                existing.status = AssetStatus.drafted
                existing.title = title
                await session.flush()
            else:
                asset = ContentAsset(
                    tenant_id=campaign.tenant_id,
                    campaign_id=campaign.id,
                    asset_type=asset_type,
                    status=AssetStatus.drafted,
                    title=title,
                    content=content_text,
                    extra_metadata=metadata,
                )
                session.add(asset)
                await session.flush()

            if channel == "linkedin_dm":
                summary = _bump(summary, linkedin_assets=1)
            else:
                summary = _bump(summary, email_assets=1)

    return summary


def _bump(
    summary: OutreachGenerationSummary,
    *,
    linkedin_assets: int = 0,
    email_assets: int = 0,
    skipped_existing: int = 0,
    error: str | None = None,
) -> OutreachGenerationSummary:
    errs = list(summary.errors)
    if error:
        errs.append(error[:200])
    return OutreachGenerationSummary(
        campaign_id=summary.campaign_id,
        audience_id=summary.audience_id,
        segments_generated=summary.segments_generated,
        linkedin_assets=summary.linkedin_assets + linkedin_assets,
        email_assets=summary.email_assets + email_assets,
        skipped_existing=summary.skipped_existing + skipped_existing,
        errors=errs,
    )


__all__ = [
    "OutreachGenerationError",
    "OutreachGenerationSummary",
    "generate_outreach_drafts",
]

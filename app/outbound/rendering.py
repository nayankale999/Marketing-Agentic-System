"""Per-contact draft rendering (W43).

Given a campaign's outreach templates and an audience, materialise one
filled-in message per (contact, channel). This happens at view time —
we never persist the per-contact text, since the templates ARE the
source of truth and tokens get re-rendered on every visit.

Why this design:
  * Storage: 5 templates × 10k contacts = 50k rows of near-duplicate
    text. Rendering at view time keeps storage small.
  * Edit-safety: an SDR can edit a template post-generation and every
    subsequent view picks up the change immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AssetType
from app.db.models import AudienceMember, ContentAsset
from app.outbound.segmentation import (
    SENIORITY_BUCKETS,
    SegmentBucket,
    segment_members,
)


_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")


# Per-contact display friendly fallbacks. If the contact has nothing
# for {first_name}, we use "there" rather than leaving "{first_name}"
# in the rendered text — looks broken.
_FIELD_FALLBACKS: dict[str, str] = {
    "first_name": "there",
    "last_name": "",
    "company": "your company",
    "title": "your role",
}


@dataclass(frozen=True)
class PersonalisedDraft:
    """One rendered message for one contact. The UI lists these
    grouped by contact so the SDR can copy/paste in order."""

    contact_email: str
    contact_name: str  # "First Last" or just first or just last
    contact_title: str
    contact_company: str
    contact_linkedin_url: str | None
    segment_key: str
    segment_label: str
    channel: str  # "linkedin_dm" or "email"
    subject: str | None  # email only
    body: str  # always set; for LinkedIn this is the DM


def _bucket_by_key(key: str) -> SegmentBucket | None:
    for b in SENIORITY_BUCKETS:
        if b.key == key:
            return b
    return None


def _render(template: str, payload: dict[str, Any]) -> str:
    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        val = payload.get(token)
        if val:
            return str(val)
        return _FIELD_FALLBACKS.get(token, "")

    return _TOKEN_RE.sub(_sub, template)


def _contact_name(payload: dict[str, Any]) -> str:
    first = (payload.get("first_name") or "").strip()
    last = (payload.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return payload.get("email", "")


async def render_personalised_drafts(
    session: AsyncSession,
    *,
    campaign_id: UUID,
    audience_id: UUID,
) -> list[PersonalisedDraft]:
    """Pull every outbound-segment ContentAsset for the campaign, then
    for each member of the audience render the (segment, channel) pair
    that matches the member's bucket. Returns drafts grouped contact-
    first so the UI can list them in a stable order."""
    members = (
        await session.execute(
            select(AudienceMember).where(AudienceMember.audience_id == audience_id)
        )
    ).scalars().all()
    if not members:
        return []

    # Pull the templates: ContentAsset rows tagged with this audience.
    assets = (
        await session.execute(
            select(ContentAsset).where(
                ContentAsset.campaign_id == campaign_id,
                ContentAsset.asset_type.in_([AssetType.linkedin_dm, AssetType.email]),
                ContentAsset.extra_metadata["audience_id"].astext == str(audience_id),
            )
        )
    ).scalars().all()
    if not assets:
        return []

    # Index by (segment_key, channel).
    by_segment: dict[tuple[str, str], ContentAsset] = {}
    for a in assets:
        meta = a.extra_metadata or {}
        seg = meta.get("segment_key")
        if not seg:
            continue
        channel = "linkedin_dm" if a.asset_type == AssetType.linkedin_dm else "email"
        by_segment[(seg, channel)] = a

    out: list[PersonalisedDraft] = []
    segs = segment_members(list(members))
    for seg_result in segs:
        bucket = seg_result.bucket
        for member in seg_result.members:
            payload = dict(member.payload or {})
            for channel in ("linkedin_dm", "email"):
                template = by_segment.get((bucket.key, channel))
                if template is None:
                    continue
                meta = template.extra_metadata or {}
                fields = meta.get("fields") or {}
                subject = (
                    _render(fields.get("subject", ""), payload)
                    if channel == "email"
                    else None
                )
                body_template = fields.get("body") or template.content or ""
                body = _render(body_template, payload)
                out.append(
                    PersonalisedDraft(
                        contact_email=payload.get("email", ""),
                        contact_name=_contact_name(payload),
                        contact_title=payload.get("title", ""),
                        contact_company=payload.get("company", ""),
                        contact_linkedin_url=payload.get("linkedin_url"),
                        segment_key=bucket.key,
                        segment_label=bucket.label,
                        channel=channel,
                        subject=subject,
                        body=body,
                    )
                )
    return out


__all__ = ["PersonalisedDraft", "render_personalised_drafts"]

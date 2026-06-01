"""Outbound personalisation pipeline (W43).

Takes a CSV-uploaded Audience, fills the gaps via Apollo, segments
contacts by seniority, and generates per-segment LinkedIn DM + email
drafts with merge tokens that render to per-contact messages at view
time.

Public surface:
  * enrichment.enrich_audience — backfill missing fields from Apollo.
  * segmentation.segment_members — bucket by seniority.
  * generation.generate_outreach_drafts — per-segment LLM drafts.
  * rendering.render_personalised_drafts — fill merge tokens per contact.
"""

from app.outbound.enrichment import (
    EnrichmentSummary,
    enrich_audience,
)
from app.outbound.generation import (
    OutreachGenerationSummary,
    generate_outreach_drafts,
)
from app.outbound.rendering import (
    PersonalisedDraft,
    render_personalised_drafts,
)
from app.outbound.segmentation import (
    SENIORITY_BUCKETS,
    SegmentBucket,
    segment_members,
)

__all__ = [
    "SENIORITY_BUCKETS",
    "EnrichmentSummary",
    "OutreachGenerationSummary",
    "PersonalisedDraft",
    "SegmentBucket",
    "enrich_audience",
    "generate_outreach_drafts",
    "render_personalised_drafts",
    "segment_members",
]

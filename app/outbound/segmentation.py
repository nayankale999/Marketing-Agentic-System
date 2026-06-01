"""Seniority-based segmentation for outbound personalisation (W43).

We bucket each AudienceMember into one of five segments so the LLM
can write segment-appropriate copy (a C-suite DM about strategic
outcomes vs an IC DM about workflow pain). Buckets are derived from
the `seniority` field (Apollo populates these consistently) with a
fallback to title-keyword matching for CSV-only rows.

Buckets are intentionally coarse — 3-5 was the design constraint;
finer cuts blow up LLM cost (one generation per bucket × per asset
type) without measurably better copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.db.models import AudienceMember


# Ordered by seniority — higher first. The first matching bucket wins
# (a "VP of Engineering" matches `vp` before falling through to
# `manager`). Each bucket's `title_keywords` is the fallback when
# Apollo's seniority field is empty.
@dataclass(frozen=True)
class SegmentBucket:
    key: str
    label: str
    seniority_values: frozenset[str]
    title_keywords: tuple[str, ...]
    talking_points: str  # Short phrase the LLM uses to anchor the angle


SENIORITY_BUCKETS: tuple[SegmentBucket, ...] = (
    SegmentBucket(
        key="c_suite",
        label="C-suite",
        seniority_values=frozenset({"c_suite", "owner", "founder", "partner"}),
        title_keywords=(
            "ceo",
            "cto",
            "cfo",
            "cmo",
            "coo",
            "cio",
            "chief ",
            "founder",
            "co-founder",
            "president",
            "owner",
        ),
        talking_points=(
            "strategic business outcomes, board-level metrics, the cost of "
            "inaction; short and high-altitude. Avoid feature lists."
        ),
    ),
    SegmentBucket(
        key="vp",
        label="VP / Head of",
        seniority_values=frozenset({"vp", "head"}),
        title_keywords=(
            "vp ",
            "vice president",
            "head of",
            "svp",
            "evp",
        ),
        talking_points=(
            "department-level impact, headcount/productivity ratios, the "
            "team's capacity story. One concrete proof point."
        ),
    ),
    SegmentBucket(
        key="director",
        label="Director",
        seniority_values=frozenset({"director", "senior_director"}),
        title_keywords=("director", "head of",),
        talking_points=(
            "operational outcomes for their function, ROI, time-to-value. "
            "Mention a peer/competitor by category if relevant."
        ),
    ),
    SegmentBucket(
        key="manager",
        label="Manager",
        seniority_values=frozenset({"manager", "senior_manager", "lead"}),
        title_keywords=("manager", "lead "),
        talking_points=(
            "team-level pain, workflow automation, what would free up "
            "their team's time this quarter."
        ),
    ),
    SegmentBucket(
        key="ic",
        label="Individual contributor",
        seniority_values=frozenset({"entry", "senior", "intern", "ic", ""}),
        title_keywords=(),
        talking_points=(
            "concrete workflow pain, hours saved per week, easy to try. "
            "Avoid corporate speak."
        ),
    ),
)


def _bucket_for_member(member: AudienceMember) -> SegmentBucket:
    payload: dict[str, Any] = dict(member.payload or {})
    seniority = (payload.get("seniority") or "").strip().lower()
    title = (payload.get("title") or "").strip().lower()

    # First pass: Apollo's seniority field.
    if seniority:
        for bucket in SENIORITY_BUCKETS:
            if seniority in bucket.seniority_values:
                return bucket

    # Fallback: keyword match on title (only if seniority was empty
    # or unrecognised). Highest-seniority bucket wins on first hit.
    # Use word-boundary regex so "coo" doesn't match "coordinator" and
    # "manager" doesn't match "management consulting".
    if title:
        for bucket in SENIORITY_BUCKETS:
            for kw in bucket.title_keywords:
                # Keywords with trailing space match the literal space —
                # treat them as substring with whitespace boundary.
                pattern = r"\b" + re.escape(kw.strip()) + r"\b"
                if re.search(pattern, title):
                    return bucket

    # No seniority and no title → IC catch-all.
    return SENIORITY_BUCKETS[-1]


@dataclass(frozen=True)
class SegmentationResult:
    bucket: SegmentBucket
    members: list[AudienceMember] = field(default_factory=list)


def segment_members(
    members: list[AudienceMember],
) -> list[SegmentationResult]:
    """Bucket the given members. Returns one SegmentationResult per
    non-empty bucket, ordered by seniority (highest first). Empty
    buckets are dropped — we don't want the LLM generating copy for
    zero recipients."""
    by_key: dict[str, list[AudienceMember]] = {}
    for m in members:
        bucket = _bucket_for_member(m)
        by_key.setdefault(bucket.key, []).append(m)

    out: list[SegmentationResult] = []
    for bucket in SENIORITY_BUCKETS:
        rows = by_key.get(bucket.key)
        if rows:
            out.append(SegmentationResult(bucket=bucket, members=rows))
    return out


__all__ = [
    "SENIORITY_BUCKETS",
    "SegmentBucket",
    "SegmentationResult",
    "segment_members",
]

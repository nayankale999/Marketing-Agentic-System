"""Segmentation primitives that power the Audience Targeting agent (E11-S04).

`estimate()` and `build()` operate against `audience_member` rows in the
caller's tenant, filtering by a small DSL:

    {
      "include": [{"field": "country", "op": "eq",  "value": "US"},
                  {"field": "tags",    "op": "has", "value": "vip"}],
      "exclude": [{"field": "tags",    "op": "has", "value": "blocked"}]
    }

`include` clauses are ANDed; `exclude` clauses are ORed into a negation. The
two top-level lists keep the JSON shape simple at the cost of one nesting
level (we add AND/OR groups in a later refinement when the UI needs them).

Both functions return DEDUPED external_ids — a contact appearing in multiple
audiences is one match. Suppression list integration (the `suppressed` /
`net` split in EstimateResult) lands with E16 unsubscribe handling; for now
`suppressed=0`.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Audience, AudienceMember

# Supported fields and the operators allowed per field. The criteria
# validator returns a structured error (and the API responds 422) if a rule
# references something outside this map.
SUPPORTED_FIELDS: dict[str, frozenset[str]] = {
    "email": frozenset({"eq", "contains"}),
    "country": frozenset({"eq", "in"}),
    "tags": frozenset({"has"}),
    "company": frozenset({"eq", "contains"}),
    "first_name": frozenset({"eq", "contains"}),
    "last_name": frozenset({"eq", "contains"}),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SegmentationError(Exception):
    """Base class for criteria-validation problems."""

    def __init__(self, message: str, *, section: str, index: int) -> None:
        super().__init__(message)
        self.section = section
        self.index = index


class FieldUnavailableError(SegmentationError):
    """Criteria references an unsupported field."""

    def __init__(self, *, section: str, index: int, field: str) -> None:
        super().__init__(
            f"{section}[{index}]: field '{field}' is not available",
            section=section,
            index=index,
        )
        self.field = field


class OperatorUnsupportedError(SegmentationError):
    """Field is known but the operator isn't valid for it."""

    def __init__(self, *, section: str, index: int, field: str, op: str) -> None:
        allowed = ", ".join(sorted(SUPPORTED_FIELDS.get(field, frozenset())))
        super().__init__(
            f"{section}[{index}]: operator '{op}' not valid for field '{field}' "
            f"(allowed: {allowed})",
            section=section,
            index=index,
        )
        self.field = field
        self.op = op


class CriteriaValueError(SegmentationError):
    """Operator was correct but value shape is wrong (e.g. `in` without a list)."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentationRule:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class SegmentationCriteria:
    include: list[SegmentationRule] = field(default_factory=list)
    exclude: list[SegmentationRule] = field(default_factory=list)


@dataclass(frozen=True)
class EstimateResult:
    total_reachable: int
    suppressed: int
    net: int


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_criteria(payload: dict[str, Any]) -> SegmentationCriteria:
    """Parse + validate. Raise the most specific SegmentationError subclass."""
    include = [
        _validate_rule(r, section="include", index=i)
        for i, r in enumerate(payload.get("include", []))
    ]
    exclude = [
        _validate_rule(r, section="exclude", index=i)
        for i, r in enumerate(payload.get("exclude", []))
    ]
    return SegmentationCriteria(include=include, exclude=exclude)


def _validate_rule(raw: Any, *, section: str, index: int) -> SegmentationRule:
    if not isinstance(raw, dict):
        raise CriteriaValueError(
            f"{section}[{index}]: rule must be an object",
            section=section,
            index=index,
        )

    field_name = raw.get("field")
    op = raw.get("op")
    value = raw.get("value")

    if not isinstance(field_name, str) or field_name not in SUPPORTED_FIELDS:
        raise FieldUnavailableError(section=section, index=index, field=str(field_name))
    if not isinstance(op, str) or op not in SUPPORTED_FIELDS[field_name]:
        raise OperatorUnsupportedError(section=section, index=index, field=field_name, op=str(op))

    if op == "in" and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
        raise CriteriaValueError(
            f"{section}[{index}]: 'in' requires a non-empty list of strings",
            section=section,
            index=index,
        )
    if op in {"eq", "contains", "has"} and (not isinstance(value, str) or not value):
        raise CriteriaValueError(
            f"{section}[{index}]: '{op}' requires a non-empty string value",
            section=section,
            index=index,
        )

    return SegmentationRule(field=field_name, op=op, value=value)


# ---------------------------------------------------------------------------
# Query building + execution
# ---------------------------------------------------------------------------


def _rule_to_condition(rule: SegmentationRule) -> Any:
    """Compile one rule against the JSONB `audience_member.payload`.

    Return type is `Any` because the JSONB column operators return loosely
    typed expressions upstream; `.where()` accepts anything boolean-ish.
    """
    column = AudienceMember.payload[rule.field]
    if rule.op == "eq":
        return column.astext == rule.value
    if rule.op == "in":
        return column.astext.in_(rule.value)
    if rule.op == "contains":
        # case-insensitive substring
        return column.astext.ilike(f"%{rule.value}%")
    if rule.op == "has":
        # array containment: payload @> {"tags": ["vip"]}
        return AudienceMember.payload.contains({rule.field: [rule.value]})
    raise NotImplementedError(f"unsupported op {rule.op}")  # pragma: no cover


def _base_query(criteria: SegmentationCriteria, tenant_id: UUID) -> Any:
    """SELECT external_id FROM audience_member JOIN audience ... WHERE ...

    Returns a SQLAlchemy Select that the caller can wrap in count() or
    pull rows from. Distinct on external_id (a contact may appear in many
    audiences).
    """
    stmt = (
        select(distinct(AudienceMember.external_id))
        .select_from(AudienceMember)
        .join(Audience, Audience.id == AudienceMember.audience_id)
        .where(Audience.tenant_id == tenant_id)
    )
    for rule in criteria.include:
        stmt = stmt.where(_rule_to_condition(rule))
    for rule in criteria.exclude:
        stmt = stmt.where(~_rule_to_condition(rule))
    return stmt


async def estimate(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    criteria: dict[str, Any],
) -> EstimateResult:
    """Count tenant-wide unique contacts that match the criteria.

    suppressed/net are placeholders until the suppression list lands; for
    now `net == total_reachable`.
    """
    parsed = validate_criteria(criteria)
    inner = _base_query(parsed, tenant_id).subquery()
    total = (await session.execute(select(func.count()).select_from(inner))).scalar_one()
    return EstimateResult(total_reachable=total, suppressed=0, net=total)


async def build(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    criteria: dict[str, Any],
) -> list[str]:
    """Return the deduped list of external_ids matching the criteria."""
    parsed = validate_criteria(criteria)
    rows = (await session.execute(_base_query(parsed, tenant_id))).scalars().all()
    return list(rows)

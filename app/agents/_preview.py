"""Asset-preview helpers for the Content Creator surface (W24, E06-S07).

Three pure pieces:

  * `resolve_merge_fields(asset_text_fields, sample_values)` — scan `{{name}}`
    placeholders in the asset's body + metadata.fields, substitute with the
    caller's sample values (falling back to built-in defaults), and report
    unresolved placeholders. Idempotent, deterministic.

  * `audit_audience_resolution(merge_fields, audience_member_payloads)` —
    for each merge field referenced in the asset, count audience members
    whose payload doesn't carry that field (AC #3 "count of unresolved").

  * `channel_constraints_for(asset_type, channel_platform)` — return the
    length budgets + required fields the preview UI uses for its mockup
    (e.g. x's 280-char counter, email's subject/preheader layout). Pulled
    from the same table the CopywritingTool uses so the UI sees exactly
    what was enforced at generation time.

No DB, no LLM — the agent module that calls these does the persistence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.tools.copywriting import _DEFAULT_LENGTH_CONSTRAINTS, _REQUIRED_FIELDS

# Built-in sample values. Used when the caller doesn't supply a value for a
# specific merge field. A polish unit can layer tenant-configurable defaults
# on top later; for MVP these cover the common cases marketers test against.
_DEFAULT_SAMPLE_VALUES: dict[str, str] = {
    "first_name": "Alex",
    "last_name": "Rivera",
    "full_name": "Alex Rivera",
    "company": "Acme Corp",
    "company_name": "Acme Corp",
    "email": "alex@example.com",
    "title": "Director of Marketing",
    "city": "London",
    "country": "United Kingdom",
    "today": "Today",
    "unsubscribe_url": "https://example.com/unsubscribe",
    "preferences_url": "https://example.com/preferences",
}

# Matches {{field_name}} with optional whitespace inside the braces. The
# captured group is the field name only — the resolver normalises case.
_MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class PreviewResolution:
    """The shape `resolve_merge_fields` returns."""

    resolved_fields: dict[str, str]
    unresolved_fields: list[str]
    referenced_fields: list[str]

    def applied_to(self, field_name: str, raw: str) -> str:
        """Apply the resolved substitutions to a single string. The resolver
        returns this per-field, but callers can also call it ad-hoc."""
        return _MERGE_FIELD_RE.sub(
            lambda m: self.resolved_fields.get(m.group(1).lower(), m.group(0)),
            raw,
        )


def extract_merge_fields(raw_strings: Iterable[str]) -> list[str]:
    """Return the union of distinct merge field names referenced anywhere in
    the input strings. Order follows first-appearance for deterministic
    test assertions."""
    seen: set[str] = set()
    out: list[str] = []
    for s in raw_strings:
        if not isinstance(s, str):
            continue
        for match in _MERGE_FIELD_RE.finditer(s):
            name = match.group(1).lower()
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def resolve_merge_fields(
    text_fields: dict[str, str],
    *,
    sample_values: dict[str, Any] | None = None,
) -> tuple[dict[str, str], PreviewResolution]:
    """Resolve every merge field in `text_fields` (keyed by field name →
    raw string). Returns `(resolved_text_fields, resolution_report)`.

    Resolution priority for each placeholder:
      1. `sample_values` (case-insensitive lookup) — caller-supplied
      2. `_DEFAULT_SAMPLE_VALUES` — built-in
      3. Unmatched — left as `{{field}}` and listed in `unresolved_fields`
    """
    caller_lookup = _normalise_keys(sample_values or {})
    referenced = extract_merge_fields(text_fields.values())

    resolved_lookup: dict[str, str] = {}
    unresolved: list[str] = []
    for name in referenced:
        if name in caller_lookup:
            resolved_lookup[name] = str(caller_lookup[name])
        elif name in _DEFAULT_SAMPLE_VALUES:
            resolved_lookup[name] = _DEFAULT_SAMPLE_VALUES[name]
        else:
            unresolved.append(name)

    rendered: dict[str, str] = {}
    for field, raw in text_fields.items():
        if not isinstance(raw, str):
            rendered[field] = raw  # leave non-strings as-is
            continue
        rendered[field] = _MERGE_FIELD_RE.sub(
            lambda m: resolved_lookup.get(m.group(1).lower(), m.group(0)),
            raw,
        )

    report = PreviewResolution(
        resolved_fields=dict(resolved_lookup),
        unresolved_fields=unresolved,
        referenced_fields=referenced,
    )
    return rendered, report


def audit_audience_resolution(
    referenced_fields: list[str],
    audience_member_payloads: Iterable[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """For each merge field, count audience members whose payload lacks it.

    Returns a map `field → {total, unresolved}`. Empty `referenced_fields`
    returns an empty dict — no fields, no audit to run."""
    if not referenced_fields:
        return {}

    counts: dict[str, dict[str, int]] = {
        name: {"total": 0, "unresolved": 0} for name in referenced_fields
    }
    for payload in audience_member_payloads:
        for name in referenced_fields:
            counts[name]["total"] += 1
            value = payload.get(name) if isinstance(payload, dict) else None
            if not _is_resolvable(value):
                counts[name]["unresolved"] += 1
    return counts


def channel_constraints_for(
    asset_type: str, channel_platform: str | None
) -> dict[str, Any]:
    """Length budgets + required-field set for the preview UI chrome.

    Pulled from the CopywritingTool's tables so the preview UI sees exactly
    what was enforced at generation time. Falls back to the asset_type's
    constraints when the channel_platform key isn't directly known."""
    keys_to_try: list[str] = []
    if channel_platform:
        keys_to_try.append(channel_platform.lower())
    keys_to_try.append(asset_type.lower())

    constraints: dict[str, int] = {}
    required: tuple[str, ...] = ()
    for key in keys_to_try:
        if key in _DEFAULT_LENGTH_CONSTRAINTS:
            constraints = dict(_DEFAULT_LENGTH_CONSTRAINTS[key])
            required = _REQUIRED_FIELDS.get(key, ())
            break

    return {
        "length_budgets": constraints,
        "required_fields": list(required),
    }


def _normalise_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Lowercase the keys so callers can pass `{"FirstName": ...}` etc."""
    return {str(k).lower(): v for k, v in d.items()}


def _is_resolvable(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


__all__ = [
    "PreviewResolution",
    "audit_audience_resolution",
    "channel_constraints_for",
    "extract_merge_fields",
    "resolve_merge_fields",
]

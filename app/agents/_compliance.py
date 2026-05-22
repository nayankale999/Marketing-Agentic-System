"""Pure compliance helpers for the Content Creator (W23, E06-S08, E06-S05 #4).

Two responsibilities:

  * `check_compliance(text, tenant_rules)` — scan a draft against the baked-in
    universal forbidden patterns AND a tenant's `ComplianceRule` rows. Returns
    a structured result with hits + a blocked flag + the words/phrases that
    should be added to a rewrite directive on retry.

  * `body_cosine_similarity(a, b)` — bag-of-words cosine for the A/B variant
    similarity gate (E06-S05 #4). Light, deterministic, no new deps. Will miss
    semantic dupes but catches the literal near-duplicates the AC targets.

The baked-in pattern list lives here (universal across tenants) so it's
versioned with the code and reviewable in PRs. Tenant-specific patterns
come from ComplianceRule rows; the agent module does the DB load.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Universal forbidden patterns. Every tenant gets these. Tightened patterns
# (e.g. medical claims for medical-vertical tenants) belong in ComplianceRule.
#
# Each entry: (label, regex, severity). Severity here is advisory; the actual
# severity applied may be tightened by a matching tenant rule.
_UNIVERSAL_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # Absolutist guarantees
    ("guaranteed_results", r"\b(?:guaranteed|guarantee|100\s*%\s*effective)\b", "block"),
    ("absolute_always_never", r"\b(?:always works|never fails|risk[\s-]?free)\b", "block"),
    # Medical/health claims (block — non-medical products can't make these)
    ("medical_claim", r"\b(?:cures?|treats?|FDA[\s-]?approved|clinically proven)\b", "block"),
    # Financial claims
    ("financial_guarantee", r"\b(?:guaranteed returns?|double your money)\b", "block"),
    # Unsupported superlatives (warn — surface but don't auto-block; common in marketing)
    ("unqualified_superlative", r"\b(?:the best|#1|the only|world[\s-]?class)\b", "warn"),
)


_RULE_KIND_EXACT = "exact"
_RULE_KIND_REGEX = "regex"
_SEVERITY_BLOCK = "block"
_SEVERITY_WARN = "warn"


@dataclass(frozen=True)
class _TenantRule:
    """Structural shape the compliance check expects for tenant rules. The
    ORM `ComplianceRule` satisfies it naturally; tests can pass a dataclass."""

    keyword: str
    pattern_kind: str
    severity: str


class ComplianceCheckError(Exception):
    """Raised when the check itself can't run (bad regex pattern, etc.) — the
    agent leaves the asset in `generating` per E06-S08 #4 rather than passing
    a draft through without a check."""


@dataclass
class ComplianceHit:
    rule: str
    severity: str
    snippet: str
    pattern_kind: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "snippet": self.snippet,
            "pattern_kind": self.pattern_kind,
        }


@dataclass
class ComplianceResult:
    hits: list[ComplianceHit] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hits

    @property
    def blocked(self) -> bool:
        return any(h.severity == _SEVERITY_BLOCK for h in self.hits)

    @property
    def warn_keywords(self) -> list[str]:
        """Keywords/patterns that need to be avoided in a rewrite-retry."""
        return [h.rule for h in self.hits if h.severity == _SEVERITY_WARN]

    def as_metadata(self, *, rewritten_for_suppression: bool = False) -> dict[str, Any]:
        return {
            "pass": self.passed,
            "blocked": self.blocked,
            "hits": [h.as_dict() for h in self.hits],
            "rewritten_for_suppression": rewritten_for_suppression,
        }


def check_compliance(
    text: str, tenant_rules: Iterable[_TenantRule]
) -> ComplianceResult:
    """Scan `text` against the universal patterns and tenant-specific rules.

    Raises `ComplianceCheckError` if a tenant rule has a malformed regex —
    the agent treats this as "tool unavailable" per E06-S08 #4."""
    result = ComplianceResult()
    if not text:
        return result

    # Universal patterns first — these are validated at import time so they
    # can't blow up here.
    for label, pattern, severity in _UNIVERSAL_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            result.hits.append(
                ComplianceHit(
                    rule=label,
                    severity=severity,
                    snippet=_snippet(text, match.start(), match.end()),
                    pattern_kind="regex",
                )
            )

    # Tenant rules. We compile and run each individually so one bad regex
    # surfaces cleanly without poisoning the rest.
    for rule in tenant_rules:
        keyword = (rule.keyword or "").strip()
        if not keyword:
            continue
        severity = rule.severity if rule.severity in {_SEVERITY_BLOCK, _SEVERITY_WARN} else _SEVERITY_WARN
        if rule.pattern_kind == _RULE_KIND_REGEX:
            try:
                regex = re.compile(rule.keyword, flags=re.IGNORECASE)
            except re.error as exc:
                raise ComplianceCheckError(
                    f"tenant compliance rule '{rule.keyword}' has invalid regex: {exc}"
                ) from exc
        else:
            regex = re.compile(r"\b" + re.escape(keyword) + r"\b", flags=re.IGNORECASE)

        for match in regex.finditer(text):
            result.hits.append(
                ComplianceHit(
                    rule=keyword,
                    severity=severity,
                    snippet=_snippet(text, match.start(), match.end()),
                    pattern_kind=rule.pattern_kind or _RULE_KIND_EXACT,
                )
            )

    return result


def body_cosine_similarity(a: str, b: str) -> float:
    """Word-bag cosine similarity in [0.0, 1.0]. Used to gate A/B variant
    differentiation (E06-S05 #4): >0.9 means the variants are too close and
    one needs to be regenerated with stronger differentiation."""
    if not a or not b:
        return 0.0
    tokens_a = _tokenise(a)
    tokens_b = _tokenise(b)
    if not tokens_a or not tokens_b:
        return 0.0

    counts_a: dict[str, int] = {}
    for t in tokens_a:
        counts_a[t] = counts_a.get(t, 0) + 1
    counts_b: dict[str, int] = {}
    for t in tokens_b:
        counts_b[t] = counts_b.get(t, 0) + 1

    shared = set(counts_a) & set(counts_b)
    dot = sum(counts_a[t] * counts_b[t] for t in shared)
    mag_a = math.sqrt(sum(c * c for c in counts_a.values()))
    mag_b = math.sqrt(sum(c * c for c in counts_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _snippet(text: str, start: int, end: int, window: int = 30) -> str:
    """Return a window of context around a match for the metadata payload."""
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + text[lo:hi] + suffix


_TOKEN_RE = re.compile(r"\b\w+\b", re.UNICODE)


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


__all__ = [
    "ComplianceCheckError",
    "ComplianceHit",
    "ComplianceResult",
    "body_cosine_similarity",
    "check_compliance",
]

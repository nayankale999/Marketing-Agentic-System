"""`seo.analysis` tool (E11-S01).

A rule-based SEO analyser the Content Creator agent calls before submitting a
draft for approval. Pure-Python, no LLM call:

  * Determinism is an AC and rule-based scoring delivers it for free — no
    temperature pinning, no respx mocks in tests.
  * The whole tool runs in microseconds, comfortably under the 5s p95 target.
  * It registers unconditionally — no ANTHROPIC_API_KEY dependency.

Scoring follows the Yoast/RankMath convention: keyword density is
(matched_word_count / total_word_count) * 100, where multi-word keywords
contribute their word count per occurrence. Penalties cover the usual SEO
hygiene checks (keyword presence, title length, meta length, body length).

Locale is accepted but v1 scoring is locale-agnostic — the rules are
character/word-count based, not language-model based, so they apply uniformly
across Latin-alphabet locales. We pass `locale` through so future extensions
(e.g. CJK word-segmentation) have a place to land without a schema change.
"""

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from app.tools.base import Tool

_MIN_WORDS_FOR_SCORE = 50
_RECOMMENDED_MIN_BODY_WORDS = 300

_TITLE_MIN_CHARS = 30
_TITLE_MAX_CHARS = 60

_META_MIN_CHARS = 120
_META_MAX_CHARS = 160
_META_TRUNCATE_CHARS = 155  # generated meta target length

_DENSITY_FLOOR_PCT = 0.3
_DENSITY_CEILING_PCT = 3.0

_PENALTY_MISSING_KEYWORD = 15
_PENALTY_KEYWORD_STUFFING = 10
_PENALTY_KEYWORD_SPARSE = 5
_PENALTY_TITLE_LENGTH = 10
_PENALTY_TITLE_NO_KEYWORD = 10
_PENALTY_BODY_SHORT = 10
_PENALTY_META_LENGTH = 5

_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


class SeoAnalysisError(Exception):
    """Tool-level error — invalid input that can't be analysed."""


@dataclass(frozen=True)
class _TitleAssessment:
    text: str
    length: int
    in_range: bool
    contains_keyword: bool
    matched_keywords: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        issues: list[str] = []
        if self.length < _TITLE_MIN_CHARS:
            issues.append("title_too_short")
        elif self.length > _TITLE_MAX_CHARS:
            issues.append("title_too_long")
        if not self.contains_keyword:
            issues.append("title_missing_target_keyword")
        return {
            "text": self.text,
            "length": self.length,
            "in_range": self.in_range,
            "contains_keyword": self.contains_keyword,
            "matched_keywords": list(self.matched_keywords),
            "issues": issues,
        }


class SeoAnalysisTool(Tool):
    name: ClassVar[str] = "seo.analysis"
    description: ClassVar[str] = (
        "Score a draft against target keywords. Returns keyword density, "
        "title/meta quality, and a list of recommendations. Deterministic."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "draft": {"type": "string"},
            "target_keywords": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "locale": {"type": "string"},
            "title": {"type": "string"},
            "meta_description": {"type": "string"},
        },
        "required": ["draft", "target_keywords"],
        "additionalProperties": True,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "score": {"type": ["integer", "null"]},
            "reason": {"type": "string"},
            "keyword_density": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "title_quality": {"type": "object"},
            "meta_description": {"type": "string"},
            "recommendations": {"type": "array", "items": {"type": "string"}},
            "word_count": {"type": "integer"},
            "locale": {"type": "string"},
        },
        "required": ["keyword_density", "title_quality", "meta_description", "recommendations"],
    }

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        draft = str(inputs.get("draft", ""))
        raw_keywords = inputs.get("target_keywords") or []
        if not isinstance(raw_keywords, list) or not raw_keywords:
            raise SeoAnalysisError("target_keywords must be a non-empty list")
        keywords = [k.strip() for k in raw_keywords if isinstance(k, str) and k.strip()]
        if not keywords:
            raise SeoAnalysisError("target_keywords must contain at least one non-empty string")

        locale = str(inputs.get("locale") or "en")
        supplied_title = inputs.get("title")
        supplied_meta = inputs.get("meta_description")

        body, extracted_title = self._split_title(draft, supplied_title)
        title_text = (
            supplied_title.strip()
            if isinstance(supplied_title, str) and supplied_title.strip()
            else extracted_title
        )

        body_words = _WORD_RE.findall(body.lower())
        word_count = len(body_words)

        if word_count < _MIN_WORDS_FOR_SCORE:
            return {
                "score": None,
                "reason": "insufficient_length",
                "keyword_density": {k: 0.0 for k in keywords},
                "title_quality": self._assess_title(title_text, keywords).as_dict(),
                "meta_description": self._resolve_meta(supplied_meta, body),
                "recommendations": [
                    f"draft has {word_count} words, need at least {_MIN_WORDS_FOR_SCORE} to score"
                ],
                "word_count": word_count,
                "locale": locale,
            }

        density = self._keyword_density(keywords, body_words)
        title_quality = self._assess_title(title_text, keywords)
        meta_text = self._resolve_meta(supplied_meta, body)

        score, recommendations = self._score(
            keywords=keywords,
            density=density,
            title=title_quality,
            word_count=word_count,
            meta_text=meta_text,
            meta_was_supplied=bool(supplied_meta),
        )

        return {
            "score": score,
            "keyword_density": density,
            "title_quality": title_quality.as_dict(),
            "meta_description": meta_text,
            "recommendations": recommendations,
            "word_count": word_count,
            "locale": locale,
        }

    @staticmethod
    def _split_title(draft: str, supplied_title: Any) -> tuple[str, str]:
        """Return (body, extracted_title). If the caller supplied a title we
        leave the draft intact; otherwise we peel the first non-empty line off
        the draft and treat the remainder as the body."""
        if isinstance(supplied_title, str) and supplied_title.strip():
            return draft, ""
        lines = draft.splitlines()
        for i, line in enumerate(lines):
            if line.strip():
                # Drop leading markdown heading markers so "# My Title" → "My Title".
                title = re.sub(r"^#+\s*", "", line.strip())[:200]
                body = "\n".join(lines[i + 1 :])
                return body, title
        return draft, ""

    @staticmethod
    def _keyword_density(
        keywords: list[str], body_words: list[str]
    ) -> dict[str, float]:
        total = len(body_words)
        if total == 0:
            return {k: 0.0 for k in keywords}
        densities: dict[str, float] = {}
        for kw in keywords:
            kw_words = _WORD_RE.findall(kw.lower())
            if not kw_words:
                densities[kw] = 0.0
                continue
            matches = _count_phrase_occurrences(body_words, kw_words)
            densities[kw] = round((matches * len(kw_words) / total) * 100, 3)
        return densities

    @staticmethod
    def _assess_title(title: str, keywords: list[str]) -> _TitleAssessment:
        length = len(title)
        in_range = _TITLE_MIN_CHARS <= length <= _TITLE_MAX_CHARS
        lowered = title.lower()
        matched = tuple(k for k in keywords if k.lower() in lowered)
        return _TitleAssessment(
            text=title,
            length=length,
            in_range=in_range,
            contains_keyword=bool(matched),
            matched_keywords=matched,
        )

    @staticmethod
    def _resolve_meta(supplied: Any, body: str) -> str:
        if isinstance(supplied, str) and supplied.strip():
            return supplied.strip()
        flat = re.sub(r"\s+", " ", body).strip()
        if len(flat) <= _META_TRUNCATE_CHARS:
            return flat
        # Truncate on a word boundary near the cap so we don't cut a word in half.
        truncated = flat[:_META_TRUNCATE_CHARS]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "…"

    @staticmethod
    def _score(
        *,
        keywords: list[str],
        density: dict[str, float],
        title: _TitleAssessment,
        word_count: int,
        meta_text: str,
        meta_was_supplied: bool,
    ) -> tuple[int, list[str]]:
        score = 100
        recs: list[str] = []

        for kw in keywords:
            d = density.get(kw, 0.0)
            if d <= 0:
                score -= _PENALTY_MISSING_KEYWORD
                recs.append(f"target keyword '{kw}' is missing from the body")
            elif d > _DENSITY_CEILING_PCT:
                score -= _PENALTY_KEYWORD_STUFFING
                recs.append(
                    f"keyword '{kw}' density is {d}% (over {_DENSITY_CEILING_PCT}%) — reduce repetition"
                )
            elif d < _DENSITY_FLOOR_PCT:
                score -= _PENALTY_KEYWORD_SPARSE
                recs.append(
                    f"keyword '{kw}' density is {d}% (under {_DENSITY_FLOOR_PCT}%) — use it more"
                )

        if not title.in_range:
            score -= _PENALTY_TITLE_LENGTH
            if title.length < _TITLE_MIN_CHARS:
                recs.append(
                    f"title is {title.length} chars; aim for {_TITLE_MIN_CHARS}-{_TITLE_MAX_CHARS}"
                )
            else:
                recs.append(
                    f"title is {title.length} chars; trim to {_TITLE_MAX_CHARS} or fewer"
                )
        if not title.contains_keyword:
            score -= _PENALTY_TITLE_NO_KEYWORD
            recs.append("title does not contain any target keyword")

        if word_count < _RECOMMENDED_MIN_BODY_WORDS:
            score -= _PENALTY_BODY_SHORT
            recs.append(
                f"body has {word_count} words; aim for at least {_RECOMMENDED_MIN_BODY_WORDS}"
            )

        # Only penalise meta length when the caller supplied one — a generated
        # meta is always within range by construction.
        if meta_was_supplied:
            meta_len = len(meta_text)
            if meta_len < _META_MIN_CHARS or meta_len > _META_MAX_CHARS:
                score -= _PENALTY_META_LENGTH
                recs.append(
                    f"meta_description is {meta_len} chars; "
                    f"aim for {_META_MIN_CHARS}-{_META_MAX_CHARS}"
                )

        return max(0, min(100, score)), recs


def _count_phrase_occurrences(haystack: list[str], needle: list[str]) -> int:
    """Count non-overlapping occurrences of `needle` (sequence of lowercased
    tokens) inside `haystack` (sequence of lowercased tokens)."""
    if not needle or len(needle) > len(haystack):
        return 0
    count = 0
    i = 0
    limit = len(haystack) - len(needle) + 1
    while i < limit:
        if haystack[i : i + len(needle)] == needle:
            count += 1
            i += len(needle)
        else:
            i += 1
    return count

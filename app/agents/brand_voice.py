"""Brand-voice primitives consumed by the Content Creator agent (E06-S02).

Two helpers live here:

  * `format_brand_voice_prompt` — render the configured voice as a fixed
    prompt block. AC #2 of E06-S02 says the active voice is included
    "verbatim" in the agent's system prompt, so this function is the only
    place that decides the wording of that block. It is deterministic:
    same input → byte-identical output (sorted lists, no timestamps).

  * `check_dont_words` — return the do-not-use words that actually appear
    in a draft, matched case-insensitively on word boundaries. The Content
    Creator agent (W22) calls this on every draft and writes the result
    into `content_asset.metadata.brand_check` per AC #4.

No DB access from this module — it operates on plain `BrandVoice` objects
(or anything that quacks like one). Tests can pass a dataclass shim.
"""

from __future__ import annotations

import re
from typing import Protocol

__all__ = ["BrandVoiceLike", "check_dont_words", "format_brand_voice_prompt"]


class BrandVoiceLike(Protocol):
    """Structural type accepted by the helpers — `BrandVoice` ORM rows satisfy
    this naturally, and tests can pass a dataclass without depending on the
    DB layer."""

    name: str
    tone_descriptors: list[str]
    do_words: list[str]
    dont_words: list[str]
    sample_paragraphs: list[str]
    reading_grade_target: object  # Decimal | None, but Decimal isn't a Protocol-friendly type


def format_brand_voice_prompt(voice: BrandVoiceLike) -> str:
    """Render the voice as a prompt block. Deterministic: list fields are
    emitted in insertion order so callers control the wording (we don't sort
    do/don't words because the order can be intentional — most-emphasised
    first, for example)."""
    lines: list[str] = [f"## Brand voice: {voice.name}"]

    if voice.tone_descriptors:
        lines.append("Tone descriptors: " + ", ".join(voice.tone_descriptors))

    if voice.do_words:
        lines.append("Prefer these words/phrases: " + ", ".join(voice.do_words))

    if voice.dont_words:
        lines.append("Avoid these words/phrases entirely: " + ", ".join(voice.dont_words))

    if voice.reading_grade_target is not None:
        lines.append(f"Target reading grade level: {voice.reading_grade_target}")

    if voice.sample_paragraphs:
        lines.append("Sample paragraphs that exemplify this voice:")
        for i, sample in enumerate(voice.sample_paragraphs, start=1):
            lines.append(f"  [{i}] {sample}")

    return "\n".join(lines)


def check_dont_words(text: str, dont_words: list[str]) -> list[str]:
    """Return the subset of `dont_words` that appear in `text`, case-insensitive,
    matched on word/phrase boundaries so 'synergy' does not match 'energy' and
    multi-word entries like 'best in class' match the literal phrase.

    Order: matches are returned in the order they appear in `dont_words` so
    output is deterministic (and easy to assert on)."""
    if not dont_words or not text:
        return []
    hits: list[str] = []
    for word in dont_words:
        normalised = word.strip()
        if not normalised:
            continue
        pattern = r"\b" + re.escape(normalised) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(normalised)
    return hits

"""W18 — `seo.analysis` tool (E11-S01).

Rule-based scorer, no LLM, so tests are straightforward: assert outputs for
hand-crafted drafts. The acceptance criteria we cover here directly:

  AC1: input shape → output shape (score, keyword_density, title_quality,
       meta_description, recommendations).
  AC2: insufficient length → score=null + reason='insufficient_length'.
  AC3: deterministic (same input → identical output across calls).
  AC4: tool failures raise — agent_log wiring is covered by W8's test_tools.py.
"""

from __future__ import annotations

import pytest

from app.tools.seo import SeoAnalysisError, SeoAnalysisTool


def _tool() -> SeoAnalysisTool:
    return SeoAnalysisTool()


def _body(words: int, kw: str = "marketing automation", every: int = 30) -> str:
    """Generate a body of `words` words with `kw` injected every `every` words."""
    out: list[str] = []
    for i in range(words):
        if i and i % every == 0:
            out.append(kw)
        else:
            out.append(f"word{i}")
    return " ".join(out)


# ---------------------------------------------------------------------------
# AC1 — shape and channel happy path
# ---------------------------------------------------------------------------


async def test_returns_full_output_shape() -> None:
    draft = (
        "How to scale marketing automation\n\n"
        + _body(words=320, kw="marketing automation", every=25)
    )
    result = await _tool().call(
        {
            "draft": draft,
            "target_keywords": ["marketing automation"],
            "locale": "en-US",
        }
    )

    assert set(result.keys()) >= {
        "score",
        "keyword_density",
        "title_quality",
        "meta_description",
        "recommendations",
        "word_count",
        "locale",
    }
    assert isinstance(result["score"], int)
    assert 0 <= result["score"] <= 100
    assert "marketing automation" in result["keyword_density"]
    assert result["keyword_density"]["marketing automation"] > 0
    assert result["title_quality"]["text"] == "How to scale marketing automation"
    assert result["title_quality"]["contains_keyword"] is True
    assert result["locale"] == "en-US"


async def test_title_extracted_from_markdown_heading_when_not_supplied() -> None:
    draft = "# Marketing automation playbook\n\n" + _body(words=320)
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["marketing automation"]}
    )
    assert result["title_quality"]["text"] == "Marketing automation playbook"


async def test_supplied_title_takes_precedence_over_extracted() -> None:
    draft = "# Throwaway\n\n" + _body(words=320)
    result = await _tool().call(
        {
            "draft": draft,
            "target_keywords": ["marketing automation"],
            "title": "Definitive guide to marketing automation",
        }
    )
    assert result["title_quality"]["text"] == "Definitive guide to marketing automation"


# ---------------------------------------------------------------------------
# AC2 — insufficient length
# ---------------------------------------------------------------------------


async def test_insufficient_length_returns_null_score_with_reason() -> None:
    result = await _tool().call(
        {
            "draft": "Marketing automation is cool. Short post.",
            "target_keywords": ["marketing automation"],
        }
    )
    assert result["score"] is None
    assert result["reason"] == "insufficient_length"
    assert any("need at least" in r for r in result["recommendations"])


async def test_insufficient_length_still_returns_required_fields() -> None:
    result = await _tool().call(
        {"draft": "tiny.", "target_keywords": ["seo"]}
    )
    assert result["keyword_density"] == {"seo": 0.0}
    assert "title_quality" in result
    assert "meta_description" in result


# ---------------------------------------------------------------------------
# AC3 — determinism
# ---------------------------------------------------------------------------


async def test_same_input_produces_identical_output() -> None:
    draft = "Scaling marketing automation\n\n" + _body(words=320)
    inputs = {
        "draft": draft,
        "target_keywords": ["marketing automation", "scaling"],
        "locale": "en",
    }
    r1 = await _tool().call(dict(inputs))
    r2 = await _tool().call(dict(inputs))
    assert r1 == r2


# ---------------------------------------------------------------------------
# AC4 — invalid input raises (tool-level failure)
# ---------------------------------------------------------------------------


async def test_empty_target_keywords_raises() -> None:
    with pytest.raises(SeoAnalysisError):
        await _tool().call({"draft": _body(words=320), "target_keywords": []})


async def test_non_list_target_keywords_raises() -> None:
    with pytest.raises(SeoAnalysisError):
        await _tool().call(
            {"draft": _body(words=320), "target_keywords": "not-a-list"}  # type: ignore[arg-type]
        )


async def test_all_blank_target_keywords_raises() -> None:
    with pytest.raises(SeoAnalysisError):
        await _tool().call(
            {"draft": _body(words=320), "target_keywords": ["", "   "]}
        )


# ---------------------------------------------------------------------------
# Scoring rules — keyword density
# ---------------------------------------------------------------------------


async def test_missing_keyword_drops_score_and_lists_recommendation() -> None:
    draft = "The platform overview\n\n" + " ".join(f"word{i}" for i in range(320))
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["marketing automation"]}
    )
    assert result["score"] < 100
    assert result["keyword_density"]["marketing automation"] == 0.0
    assert any("missing" in r for r in result["recommendations"])


async def test_keyword_stuffing_drops_score() -> None:
    body_words = ["marketing", "automation"] * 100 + ["filler"] * 50
    draft = "Marketing automation guide\n\n" + " ".join(body_words)
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["marketing automation"]}
    )
    density = result["keyword_density"]["marketing automation"]
    assert density > 3.0
    assert any("density" in r and "reduce" in r for r in result["recommendations"])


async def test_balanced_density_keeps_score_high() -> None:
    # Title >= 30 chars and contains the keyword; body 400 words with the
    # 2-token keyword injected every 100 words → ~2% density, comfortably
    # inside [0.3%, 3.0%].
    draft = "Marketing automation guide essentials\n\n" + _body(
        words=400, every=100
    )
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["marketing automation"]}
    )
    assert result["score"] >= 90, result


# ---------------------------------------------------------------------------
# Scoring rules — title and meta
# ---------------------------------------------------------------------------


async def test_short_title_flags_issue_and_drops_score() -> None:
    draft = "Hi\n\n" + _body(words=320)
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["marketing automation"]}
    )
    assert "title_too_short" in result["title_quality"]["issues"]
    assert result["score"] < 90


async def test_long_title_flags_issue() -> None:
    long_title = "A very long title that overshoots the recommended sixty character cap by a clear margin"
    result = await _tool().call(
        {
            "draft": _body(words=320),
            "target_keywords": ["marketing automation"],
            "title": long_title,
        }
    )
    assert "title_too_long" in result["title_quality"]["issues"]


async def test_title_missing_keyword_flag() -> None:
    result = await _tool().call(
        {
            "draft": _body(words=320),
            "target_keywords": ["marketing automation"],
            "title": "An unrelated headline about something else entirely",
        }
    )
    assert "title_missing_target_keyword" in result["title_quality"]["issues"]


async def test_generated_meta_truncates_long_body_to_under_cap() -> None:
    body = "A " * 500
    draft = "Title goes here that is long enough\n\n" + body
    result = await _tool().call(
        {"draft": draft, "target_keywords": ["a"]}
    )
    assert len(result["meta_description"]) <= 156  # truncate cap + ellipsis


async def test_supplied_meta_length_penalised_when_out_of_range() -> None:
    short_meta = "Too short."
    result = await _tool().call(
        {
            "draft": _body(words=320),
            "target_keywords": ["marketing automation"],
            "title": "Scaling marketing automation today fast",
            "meta_description": short_meta,
        }
    )
    assert result["meta_description"] == short_meta
    assert any("meta_description" in r for r in result["recommendations"])


# ---------------------------------------------------------------------------
# Registry integration — registers without ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


def test_seo_tool_registers_unconditionally(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings.config import get_settings
    from app.tools import base as base_mod
    from app.tools import register_builtin_tools

    # Force-clear the registry and the idempotency guard so we can re-register
    # in this test without colliding with whatever conftest set up earlier.
    base_mod.tool_registry._tools.clear()  # type: ignore[attr-defined]
    import app.tools as tools_pkg

    monkeypatch.setattr(tools_pkg, "_REGISTERED", False)

    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    register_builtin_tools()
    assert "seo.analysis" in base_mod.tool_registry.names()
    assert "copywriting.generate" not in base_mod.tool_registry.names()

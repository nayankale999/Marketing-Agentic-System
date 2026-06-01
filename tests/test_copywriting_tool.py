"""W17 — `copywriting.generate` tool (E11-S02).

All Anthropic API traffic is intercepted via respx. The tool is exercised
directly (not via the orchestrator) because the channel/length/retry behaviour
is what we care about here; the agent_log wiring is already covered by W8's
test_tools.py through the generic tool handler.
"""

import json

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic

from app.tools.copywriting import CopywritingError, CopywritingTool

_API = "https://api.anthropic.com/v1/messages"


def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key="test-key")


def _tool() -> CopywritingTool:
    return CopywritingTool(client=_client(), model="claude-sonnet-4-6")


def _msg(payload: dict[str, object]) -> httpx.Response:
    """Build an Anthropic Messages API response wrapping a JSON string body."""
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


# ---------------------------------------------------------------------------
# Happy paths — per channel
# ---------------------------------------------------------------------------


@respx.mock
async def test_email_returns_subject_preheader_body_cta() -> None:
    respx.post(_API).mock(
        return_value=_msg(
            {
                "subject": "Cut deploys to 5 min",
                "preheader": "How Acme shipped 3x faster",
                "body": "Hi {first_name}, our pipeline rewrite trimmed ...",
                "cta": "Book a demo",
            }
        )
    )
    out = await _tool().call(
        {
            "channel": "email",
            "asset_type": "nurture",
            "brief": "Promote pipeline rewrite case study",
        }
    )
    assert out["subject"] == "Cut deploys to 5 min"
    assert out["preheader"] == "How Acme shipped 3x faster"
    assert out["body"].startswith("Hi {first_name}")
    assert out["cta"] == "Book a demo"
    assert out["length_metrics"]["subject"] == len("Cut deploys to 5 min")
    assert "length_warning" not in out


@respx.mock
async def test_linkedin_returns_headline_body_cta_no_subject() -> None:
    respx.post(_API).mock(
        return_value=_msg(
            {
                "headline": "Faster pipelines, less yak-shaving",
                "body": "We rewrote ours top-to-bottom. Three lessons learned ...",
                "cta": "Read the writeup",
            }
        )
    )
    out = await _tool().call(
        {"channel": "linkedin", "asset_type": "post", "brief": "Share pipeline rewrite"}
    )
    assert "subject" not in out
    assert out["headline"].startswith("Faster pipelines")
    assert out["cta"] == "Read the writeup"


@respx.mock
async def test_x_channel_within_280() -> None:
    respx.post(_API).mock(
        return_value=_msg({"body": "Shipped 3x faster after the pipeline rewrite. Thread ↓"})
    )
    out = await _tool().call(
        {"channel": "x", "asset_type": "tweet", "brief": "Tease the case study"}
    )
    assert len(out["body"]) <= 280
    assert "length_warning" not in out


@respx.mock
async def test_ad_creative_requires_headline_and_primary_text() -> None:
    respx.post(_API).mock(
        return_value=_msg(
            {
                "headline": "5-min deploys",
                "body": "From 60 min to 5",
                "primary_text": "Acme cut deploy time by 92%. Here's how.",
            }
        )
    )
    out = await _tool().call(
        {"channel": "ad_creative", "asset_type": "meta_ad", "brief": "Promote case study"}
    )
    assert out["headline"]
    assert out["primary_text"]
    assert out["body"]


# ---------------------------------------------------------------------------
# Required-field enforcement
# ---------------------------------------------------------------------------


@respx.mock
async def test_email_without_subject_raises() -> None:
    respx.post(_API).mock(
        return_value=_msg({"preheader": "p", "body": "b", "cta": "c"})  # no subject
    )
    with pytest.raises(CopywritingError, match="subject"):
        await _tool().call(
            {"channel": "email", "asset_type": "nurture", "brief": "anything"}
        )


@respx.mock
async def test_email_without_preheader_raises() -> None:
    respx.post(_API).mock(
        return_value=_msg({"subject": "s", "body": "b", "cta": "c"})  # no preheader
    )
    with pytest.raises(CopywritingError, match="preheader"):
        await _tool().call(
            {"channel": "email", "asset_type": "nurture", "brief": "anything"}
        )


@respx.mock
async def test_unsupported_channel_raises() -> None:
    with pytest.raises(CopywritingError, match="unsupported channel"):
        await _tool().call(
            {"channel": "fax_machine", "asset_type": "x", "brief": "y"}
        )


# ---------------------------------------------------------------------------
# Length constraints + retry behaviour
# ---------------------------------------------------------------------------


@respx.mock
async def test_overlong_attempts_get_length_warning_after_two_retries() -> None:
    """All three attempts overrun the body budget. Tool returns the best
    attempt plus a `length_warning` rather than raising."""
    long_body = "x" * 320  # caller cap is 280 below
    route = respx.post(_API).mock(return_value=_msg({"body": long_body}))

    out = await _tool().call(
        {
            "channel": "x",
            "asset_type": "tweet",
            "brief": "Make it short",
            "length_constraints": {"body": 280},
        }
    )

    assert route.call_count == 3  # initial + 2 retries
    assert out["body"] == long_body
    assert out["length_warning"] == {"body": 40}
    assert out["length_metrics"]["body"] == 320


@respx.mock
async def test_retry_succeeds_on_second_attempt() -> None:
    """First attempt overruns; tool retries; second attempt fits — no warning."""
    long = _msg({"body": "y" * 300})
    short = _msg({"body": "y" * 200})
    # respx returns side-effects in order via side_effect.
    route = respx.post(_API).mock(side_effect=[long, short])

    out = await _tool().call(
        {
            "channel": "x",
            "asset_type": "tweet",
            "brief": "Trim it",
            "length_constraints": {"body": 280},
        }
    )

    assert route.call_count == 2
    assert "length_warning" not in out
    assert out["length_metrics"]["body"] == 200


@respx.mock
async def test_retry_picks_best_attempt_when_all_overflow() -> None:
    """Three attempts, decreasing overrun. Tool surfaces the closest-to-budget one."""
    a = _msg({"body": "z" * 320})
    b = _msg({"body": "z" * 310})
    c = _msg({"body": "z" * 305})
    route = respx.post(_API).mock(side_effect=[a, b, c])

    out = await _tool().call(
        {
            "channel": "x",
            "asset_type": "tweet",
            "brief": "Tighter",
            "length_constraints": {"body": 280},
        }
    )

    assert route.call_count == 3
    assert out["length_metrics"]["body"] == 305  # best (smallest overrun)
    assert out["length_warning"] == {"body": 25}


@respx.mock
async def test_retry_prompt_includes_overrun_hint() -> None:
    overflow = _msg({"body": "w" * 320})
    fix = _msg({"body": "w" * 200})
    route = respx.post(_API).mock(side_effect=[overflow, fix])

    await _tool().call(
        {
            "channel": "x",
            "asset_type": "tweet",
            "brief": "Trim",
            "length_constraints": {"body": 280},
        }
    )

    # Inspect the second request's body — it should reference the overrun.
    assert route.call_count == 2
    second_req = route.calls[1].request
    payload = json.loads(second_req.content)
    user_prompt = payload["messages"][0]["content"]
    assert "previous attempt exceeded" in user_prompt
    assert "body by 40 chars" in user_prompt


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@respx.mock
async def test_same_seed_produces_identical_api_requests() -> None:
    """E11-S02 determinism criterion. The model is mocked, so we verify the
    REQUEST sent to Anthropic is byte-identical for identical inputs."""
    response = _msg(
        {"subject": "s", "preheader": "p", "body": "b", "cta": "c"}
    )
    route = respx.post(_API).mock(return_value=response)

    inputs = {
        "channel": "email",
        "asset_type": "nurture",
        "brief": "X",
        "seed": 42,
    }
    out1 = await _tool().call(dict(inputs))
    out2 = await _tool().call(dict(inputs))

    assert route.call_count == 2
    body1 = route.calls[0].request.content
    body2 = route.calls[1].request.content
    assert body1 == body2
    assert out1 == out2


@respx.mock
async def test_seed_pins_temperature_to_zero() -> None:
    route = respx.post(_API).mock(
        return_value=_msg(
            {"subject": "s", "preheader": "p", "body": "b", "cta": "c"}
        )
    )
    await _tool().call(
        {
            "channel": "email",
            "asset_type": "nurture",
            "brief": "Promo",
            "seed": "abc",
        }
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["temperature"] == 0.0


@respx.mock
async def test_no_seed_uses_nonzero_temperature() -> None:
    route = respx.post(_API).mock(
        return_value=_msg(
            {"subject": "s", "preheader": "p", "body": "b", "cta": "c"}
        )
    )
    await _tool().call(
        {"channel": "email", "asset_type": "nurture", "brief": "Promo"}
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["temperature"] > 0.0


# ---------------------------------------------------------------------------
# Output parsing edge cases
# ---------------------------------------------------------------------------


@respx.mock
async def test_tolerates_code_fence_around_json() -> None:
    response = httpx.Response(
        200,
        json={
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [
                {
                    "type": "text",
                    "text": "```json\n"
                    + json.dumps(
                        {"subject": "s", "preheader": "p", "body": "b", "cta": "c"}
                    )
                    + "\n```",
                }
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    respx.post(_API).mock(return_value=response)
    out = await _tool().call(
        {"channel": "email", "asset_type": "nurture", "brief": "X"}
    )
    assert out["subject"] == "s"


@respx.mock
async def test_invalid_json_response_raises() -> None:
    response = httpx.Response(
        200,
        json={
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "not json at all"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    respx.post(_API).mock(return_value=response)
    with pytest.raises(CopywritingError, match="not valid JSON"):
        await _tool().call(
            {"channel": "email", "asset_type": "nurture", "brief": "X"}
        )


# ---------------------------------------------------------------------------
# Conditional registration on api key
# ---------------------------------------------------------------------------


def test_tool_registers_when_anthropic_api_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry should expose `copywriting.generate` when the env carries
    an api key. We reset the module-level _REGISTERED flag and the global
    registry, then re-run register_builtin_tools()."""
    from app.settings.config import get_settings
    from app.tools import base as tools_base
    from app.tools import register_builtin_tools

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()

    import app.tools as tools_pkg

    tools_pkg._REGISTERED = False
    tools_base.tool_registry = tools_base.ToolRegistry()
    # Re-bind the registry the module imports
    tools_pkg.tool_registry = tools_base.tool_registry

    register_builtin_tools()
    assert "copywriting.generate" in tools_base.tool_registry.names()

    # Cleanup so we don't poison sibling tests.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    tools_pkg._REGISTERED = False
    tools_base.tool_registry = tools_base.ToolRegistry()
    tools_pkg.tool_registry = tools_base.tool_registry


def test_tool_skipped_when_no_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings.config import Settings, get_settings
    from app.tools import base as tools_base
    from app.tools import register_builtin_tools

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # `.env.local` may have the key set during dev work — bypass file
    # loading entirely so this test stays deterministic. We patch both
    # the canonical reference and the one app.tools imported at module
    # load time.
    fake = lambda: Settings(_env_file=None, anthropic_api_key="")
    monkeypatch.setattr("app.settings.config.get_settings", fake)
    monkeypatch.setattr("app.tools.get_settings", fake)
    get_settings.cache_clear()

    import app.tools as tools_pkg

    tools_pkg._REGISTERED = False
    tools_base.tool_registry = tools_base.ToolRegistry()
    tools_pkg.tool_registry = tools_base.tool_registry

    register_builtin_tools()
    assert "copywriting.generate" not in tools_base.tool_registry.names()

    # Cleanup.
    get_settings.cache_clear()
    tools_pkg._REGISTERED = False
    tools_base.tool_registry = tools_base.ToolRegistry()
    tools_pkg.tool_registry = tools_base.tool_registry

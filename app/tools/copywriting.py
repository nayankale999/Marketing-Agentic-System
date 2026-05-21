"""`copywriting.generate` tool (E11-S02).

A single tool the Content Creator agent calls instead of building channel-specific
prompts inline. Constraints handled here so the agent stays generic:

  * Channel-specific required fields (email needs subject + preheader; x/twitter
    has a hard 280-char body cap; ad_creative has headline + primary_text).
  * Per-field length budgets supplied by the caller — if any field overruns we
    retry up to twice with a tighter prompt, then surface the best attempt with
    a `length_warning` rather than failing hard.
  * Determinism — when the caller passes `seed`, temperature is pinned to 0 and
    the seed is included verbatim in the prompt so identical inputs produce
    identical API requests (and therefore identical responses, in tests against
    a mocked Anthropic endpoint).

The tool depends on an injected `anthropic.AsyncAnthropic`. Tests use respx to
intercept api.anthropic.com; in prod the registry wires up a real client only
if `ANTHROPIC_API_KEY` is set.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock

from app.tools.base import Tool

# Default per-channel defaults applied when the caller doesn't supply
# length_constraints. These are sane upper bounds, not hard platform limits
# (twitter/x is the one true hard cap — enforced separately below).
_DEFAULT_LENGTH_CONSTRAINTS: dict[str, dict[str, int]] = {
    "email": {"subject": 78, "preheader": 110, "body": 1500, "cta": 40},
    "linkedin": {"headline": 150, "body": 1300, "cta": 40},
    "x": {"body": 280},
    "twitter": {"body": 280},
    "ad_creative": {"headline": 40, "body": 90, "cta": 30, "primary_text": 125},
}

# Fields that MUST be present in the model output for each channel. The model
# may include extras (e.g. headline on email) which we keep but don't require.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "email": ("subject", "preheader", "body", "cta"),
    "linkedin": ("headline", "body", "cta"),
    "x": ("body",),
    "twitter": ("body",),
    "ad_creative": ("headline", "body", "primary_text"),
}

_MAX_RETRIES = 2


class CopywritingError(Exception):
    """Tool-level error — malformed model output or unsupported channel."""


@dataclass(frozen=True)
class _Attempt:
    fields: dict[str, str]
    length_metrics: dict[str, int]
    over_limit: dict[str, int]  # field -> chars over the budget


class CopywritingTool(Tool):
    name: ClassVar[str] = "copywriting.generate"
    description: ClassVar[str] = (
        "Generate channel-specific marketing copy. Handles email/linkedin/x/"
        "ad_creative with per-field length budgets + automatic retry on overrun."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "enum": list(_REQUIRED_FIELDS.keys())},
            "asset_type": {"type": "string"},
            "brief": {"type": "string"},
            "voice": {"type": "string"},
            "audience_summary": {"type": "string"},
            "length_constraints": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 1},
            },
            "seed": {"type": ["string", "integer"]},
        },
        "required": ["channel", "asset_type", "brief"],
        "additionalProperties": True,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "preheader": {"type": "string"},
            "headline": {"type": "string"},
            "body": {"type": "string"},
            "cta": {"type": "string"},
            "primary_text": {"type": "string"},
            "length_metrics": {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
            "length_warning": {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
        },
        "required": ["body", "length_metrics"],
    }

    def __init__(self, *, client: AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # Strip the handler-injected attempt marker so it doesn't leak into the prompt.
        clean = {k: v for k, v in inputs.items() if k != "__attempt__"}
        channel = str(clean.get("channel", "")).lower()
        if channel not in _REQUIRED_FIELDS:
            raise CopywritingError(f"unsupported channel '{channel}'")

        constraints = self._resolve_constraints(channel, clean.get("length_constraints"))
        seed = clean.get("seed")

        best: _Attempt | None = None
        for attempt in range(_MAX_RETRIES + 1):
            tightening = best.over_limit if (best and attempt > 0) else None
            prompt = self._build_prompt(
                channel=channel,
                asset_type=str(clean.get("asset_type", "")),
                brief=str(clean.get("brief", "")),
                voice=clean.get("voice"),
                audience_summary=clean.get("audience_summary"),
                constraints=constraints,
                seed=seed,
                tightening=tightening,
            )
            raw = await self._call_model(prompt=prompt, seed=seed)
            fields = self._parse_response(raw, channel=channel)
            attempt_obj = self._measure(fields, constraints)

            if not attempt_obj.over_limit:
                best = attempt_obj
                break
            if best is None or sum(attempt_obj.over_limit.values()) < sum(best.over_limit.values()):
                best = attempt_obj

        assert best is not None  # loop always sets it at least once
        result: dict[str, Any] = dict(best.fields)
        result["length_metrics"] = dict(best.length_metrics)
        if best.over_limit:
            result["length_warning"] = dict(best.over_limit)
        return result

    @staticmethod
    def _resolve_constraints(
        channel: str, caller_supplied: Any
    ) -> dict[str, int]:
        defaults = dict(_DEFAULT_LENGTH_CONSTRAINTS.get(channel, {}))
        if isinstance(caller_supplied, dict):
            for k, v in caller_supplied.items():
                if isinstance(v, int) and v > 0:
                    defaults[k] = v
        return defaults

    @staticmethod
    def _build_prompt(
        *,
        channel: str,
        asset_type: str,
        brief: str,
        voice: Any,
        audience_summary: Any,
        constraints: dict[str, int],
        seed: Any,
        tightening: dict[str, int] | None,
    ) -> str:
        required = _REQUIRED_FIELDS[channel]
        budget_lines = "\n".join(
            f"  - {field}: <= {max_chars} characters" for field, max_chars in constraints.items()
        )
        lines = [
            f"You are writing {asset_type} copy for the {channel} channel.",
            f"Brief:\n{brief}",
        ]
        if voice:
            lines.append(f"Brand voice: {voice}")
        if audience_summary:
            lines.append(f"Audience: {audience_summary}")
        if seed is not None:
            lines.append(f"Determinism seed (do not echo): {seed}")
        lines.append(f"Required output fields: {', '.join(required)}")
        lines.append(f"Length budgets:\n{budget_lines}")
        if tightening:
            overruns = ", ".join(f"{field} by {over} chars" for field, over in tightening.items())
            lines.append(
                "Your previous attempt exceeded the budget on: "
                f"{overruns}. Tighten those fields this time."
            )
        lines.append(
            "Respond with a SINGLE JSON object containing exactly the required "
            "fields above (string values only). No prose, no markdown, no code fences."
        )
        return "\n\n".join(lines)

    async def _call_model(self, *, prompt: str, seed: Any) -> str:
        # Pin temperature to 0 when a seed is supplied so identical inputs
        # produce identical API requests. Otherwise allow some creative drift.
        temperature = 0.0 if seed is not None else 0.7
        message: Message = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in message.content:
            if isinstance(block, TextBlock):
                return block.text
        raise CopywritingError("model returned no text content")

    @staticmethod
    def _parse_response(raw: str, *, channel: str) -> dict[str, str]:
        text = raw.strip()
        # Be tolerant of accidental code fences even though we asked for none.
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CopywritingError(f"model output was not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise CopywritingError("model output was not a JSON object")

        fields: dict[str, str] = {}
        for k, v in obj.items():
            if isinstance(v, str):
                fields[k] = v
        missing = [f for f in _REQUIRED_FIELDS[channel] if not fields.get(f)]
        if missing:
            raise CopywritingError(
                f"model output missing required field(s) for {channel}: {missing}"
            )
        return fields

    @staticmethod
    def _measure(fields: dict[str, str], constraints: dict[str, int]) -> _Attempt:
        metrics = {f: len(v) for f, v in fields.items()}
        over = {
            f: metrics[f] - constraints[f]
            for f in constraints
            if f in metrics and metrics[f] > constraints[f]
        }
        return _Attempt(fields=fields, length_metrics=metrics, over_limit=over)

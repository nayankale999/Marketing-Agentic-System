"""LLM-backed planner for the Campaign Strategist (W20, E05-S01/02/05).

The planner is intentionally narrow: build a prompt, call Anthropic, parse the
JSON response, validate against tenant constraints + caller overrides, retry
on violation up to `_MAX_RETRIES` times. It does not touch the DB — the agent
module is what wraps a tenant context around it.

The validator is the contract: it's what the AC actually says. The prompt is
just a hint to the model; the validator enforces.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock

_MAX_RETRIES = 2
_ALLOCATION_PCT_TOLERANCE = Decimal("1.0")  # percent points
_BUDGET_PER_CHANNEL_TOLERANCE = Decimal("0.50")  # currency units, per channel (rounding slack)


class StrategistError(Exception):
    """Tool-level error — model output was unrecoverable after retries."""


@dataclass(frozen=True)
class ChannelInfo:
    """A real channel configured for the tenant."""

    platform: str
    name: str


@dataclass(frozen=True)
class HumanOverride:
    """A hard constraint the caller wants honoured verbatim on re-plan.

    Carrying both `allocation_pct` and `allocation_amount` lets the validator
    accept whichever the model echoes back — they should agree but the model
    occasionally rounds one and not the other."""

    platform: str
    allocation_pct: Decimal
    allocation_amount: Decimal


@dataclass(frozen=True)
class StrategyContext:
    """All the inputs the planner needs. Tests can build one directly."""

    campaign_name: str
    campaign_type: str
    objective: str
    brief: str | None
    budget_total: Decimal
    currency: str
    start_date: str  # ISO-8601, kept as str so the prompt block is stable
    end_date: str
    audience_size: int
    audience_summary: str
    available_channels: list[ChannelInfo]
    forbidden_platforms: list[str] = field(default_factory=list)
    hard_caps: list[dict[str, Any]] = field(default_factory=list)
    human_overrides: list[HumanOverride] = field(default_factory=list)


@dataclass(frozen=True)
class PlannerResult:
    payload: dict[str, Any]
    validation_warnings: list[dict[str, Any]]
    attempts: int


class StrategistPlanner:
    def __init__(self, *, client: AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def propose(self, ctx: StrategyContext) -> PlannerResult:
        """Run the LLM, validate, retry on violation, return the best attempt.

        `attempts` on the returned result is the TOTAL number of model calls
        made — useful for telemetry. The "best" attempt is whichever produced
        the fewest violations; on ties we keep the earliest."""
        if not ctx.available_channels:
            raise StrategistError("no active channels configured for this tenant")
        if ctx.budget_total <= 0:
            raise StrategistError("campaign budget_total must be positive")

        allowed = self._allowed_platforms(ctx)
        if not allowed:
            raise StrategistError("every configured channel is forbidden by tenant constraints")

        best_payload: dict[str, Any] | None = None
        best_violations: list[dict[str, Any]] = []
        last_violations: list[dict[str, Any]] = []
        attempts_made = 0

        for _ in range(_MAX_RETRIES + 1):
            prompt = self._build_prompt(ctx, retry_violations=last_violations)
            raw = await self._call_model(prompt)
            attempts_made += 1
            payload = self._parse_response(raw)
            violations = self._validate(payload, ctx, allowed_platforms=allowed)

            if not violations:
                return PlannerResult(
                    payload=payload, validation_warnings=[], attempts=attempts_made
                )

            if best_payload is None or len(violations) < len(best_violations):
                best_payload = payload
                best_violations = violations
            last_violations = violations

        assert best_payload is not None
        return PlannerResult(
            payload=best_payload,
            validation_warnings=best_violations,
            attempts=attempts_made,
        )

    @staticmethod
    def _allowed_platforms(ctx: StrategyContext) -> set[str]:
        forbidden = {p.lower() for p in ctx.forbidden_platforms}
        return {c.platform.lower() for c in ctx.available_channels if c.platform.lower() not in forbidden}

    def _build_prompt(
        self, ctx: StrategyContext, *, retry_violations: list[dict[str, Any]]
    ) -> str:
        channels_block = "\n".join(
            f"  - {c.platform} ({c.name})" for c in ctx.available_channels
        ) or "  (none)"
        forbidden_block = (
            "\n".join(f"  - {p}" for p in ctx.forbidden_platforms)
            if ctx.forbidden_platforms
            else "  (none)"
        )
        caps_block = (
            "\n".join(f"  - {json.dumps(c)}" for c in ctx.hard_caps)
            if ctx.hard_caps
            else "  (none)"
        )
        overrides_block = (
            "\n".join(
                f"  - {o.platform}: allocation_pct={o.allocation_pct}, "
                f"allocation_amount={o.allocation_amount}"
                for o in ctx.human_overrides
            )
            if ctx.human_overrides
            else "  (none)"
        )

        lines = [
            "You are the Campaign Strategist agent. Propose a concrete plan: channel mix, ",
            "budget allocation, and KPI targets. Every choice needs a one-sentence rationale.",
            "",
            f"Campaign: {ctx.campaign_name}",
            f"Type: {ctx.campaign_type}",
            f"Objective: {ctx.objective}",
        ]
        if ctx.brief:
            lines.append(f"Brief: {ctx.brief}")
        lines.extend(
            [
                f"Dates: {ctx.start_date} to {ctx.end_date}",
                f"Budget total: {ctx.budget_total} {ctx.currency}",
                f"Audience: {ctx.audience_size} contacts — {ctx.audience_summary}",
                "",
                "Active channels (you may only propose these):",
                channels_block,
                "",
                "Forbidden channels (NEVER propose these):",
                forbidden_block,
                "",
                "Hard caps (for context — calendar enforcement lands later):",
                caps_block,
                "",
                "Human overrides (MUST appear in your plan at these allocations, "
                "marked human_override=true):",
                overrides_block,
            ]
        )
        if retry_violations:
            lines.extend(
                [
                    "",
                    "Your previous attempt violated these rules — fix them this time:",
                ]
            )
            lines.extend(f"  - {v['kind']}: {v['detail']}" for v in retry_violations)

        lines.extend(
            [
                "",
                "Respond with a SINGLE JSON object. No prose, no markdown, no code fences.",
                "Schema:",
                "{",
                '  "channels": [',
                "    {",
                '      "platform": "<one of the active channel platforms>",',
                '      "name": "<friendly label>",',
                '      "allocation_pct": <number 0-100>,',
                '      "allocation_amount": "<decimal string matching pct * budget_total / 100>",',
                '      "rationale": "<one sentence>",',
                '      "human_override": <bool>',
                "    }",
                "  ],",
                '  "kpis": {',
                '    "primary": {"metric": "<metric_id>", "target": <number>, "rationale": "<one sentence>"},',
                '    "secondary": [{"metric": "<metric_id>", "target": <number>, "rationale": "<one sentence>"}]',
                "  },",
                '  "summary_rationale": "<2-3 sentences explaining the overall plan>"',
                "}",
                "",
                "Constraints:",
                "  - allocation_pct values across channels MUST sum to 100 (±1 for rounding).",
                "  - allocation_amount per channel MUST equal allocation_pct/100 * budget_total.",
                "  - Every channel rationale MUST be non-empty.",
                "  - primary KPI must have a non-empty rationale.",
            ]
        )
        return "\n".join(lines)

    async def _call_model(self, prompt: str) -> str:
        message: Message = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in message.content:
            if isinstance(block, TextBlock):
                return block.text
        raise StrategistError("model returned no text content")

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        text = raw.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StrategistError(f"model output was not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise StrategistError("model output was not a JSON object")
        return obj

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        ctx: StrategyContext,
        *,
        allowed_platforms: set[str],
    ) -> list[dict[str, Any]]:
        """Return a list of violation dicts. Empty list = valid."""
        violations: list[dict[str, Any]] = []
        channels = payload.get("channels")

        if not isinstance(channels, list) or not channels:
            violations.append({"kind": "shape", "detail": "channels must be a non-empty list"})
            return violations

        seen_platforms: set[str] = set()
        total_pct = Decimal("0")
        total_amount = Decimal("0")
        forbidden = {p.lower() for p in ctx.forbidden_platforms}

        for i, ch in enumerate(channels):
            if not isinstance(ch, dict):
                violations.append(
                    {"kind": "shape", "detail": f"channels[{i}] must be an object"}
                )
                continue
            platform = str(ch.get("platform", "")).lower()
            if not platform:
                violations.append(
                    {"kind": "shape", "detail": f"channels[{i}].platform is required"}
                )
                continue
            if platform in forbidden:
                violations.append(
                    {
                        "kind": "forbidden_channel",
                        "detail": f"channels[{i}].platform '{platform}' is forbidden",
                    }
                )
                continue
            if platform not in allowed_platforms:
                violations.append(
                    {
                        "kind": "unknown_channel",
                        "detail": (
                            f"channels[{i}].platform '{platform}' is not an active "
                            f"channel for this tenant"
                        ),
                    }
                )
                continue
            if platform in seen_platforms:
                violations.append(
                    {
                        "kind": "duplicate_channel",
                        "detail": f"channels[{i}].platform '{platform}' is listed twice",
                    }
                )
                continue
            seen_platforms.add(platform)

            try:
                pct = Decimal(str(ch.get("allocation_pct")))
                amount = Decimal(str(ch.get("allocation_amount")))
            except (ArithmeticError, TypeError, ValueError):
                violations.append(
                    {
                        "kind": "shape",
                        "detail": f"channels[{i}] allocation_pct/amount must be numeric",
                    }
                )
                continue
            if pct < 0 or pct > 100:
                violations.append(
                    {
                        "kind": "out_of_range",
                        "detail": f"channels[{i}].allocation_pct={pct} outside 0..100",
                    }
                )
            if amount < 0:
                violations.append(
                    {
                        "kind": "out_of_range",
                        "detail": f"channels[{i}].allocation_amount={amount} negative",
                    }
                )

            expected_amount = (ctx.budget_total * pct / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if abs(amount - expected_amount) > _BUDGET_PER_CHANNEL_TOLERANCE:
                violations.append(
                    {
                        "kind": "amount_mismatch",
                        "detail": (
                            f"channels[{i}].allocation_amount={amount} does not match "
                            f"pct*budget_total={expected_amount}"
                        ),
                    }
                )

            if not str(ch.get("rationale", "")).strip():
                violations.append(
                    {
                        "kind": "missing_rationale",
                        "detail": f"channels[{i}].rationale is empty",
                    }
                )

            total_pct += pct
            total_amount += amount

        if abs(total_pct - Decimal("100")) > _ALLOCATION_PCT_TOLERANCE:
            violations.append(
                {
                    "kind": "allocation_sum",
                    "detail": f"allocation_pct sum is {total_pct}, must be 100 (±1)",
                }
            )

        # Human overrides — every override platform must appear with matching pct.
        for ov in ctx.human_overrides:
            target_platform = ov.platform.lower()
            match = next(
                (
                    ch
                    for ch in channels
                    if isinstance(ch, dict) and str(ch.get("platform", "")).lower() == target_platform
                ),
                None,
            )
            if match is None:
                violations.append(
                    {
                        "kind": "missing_override",
                        "detail": (
                            f"human override for '{target_platform}' was dropped — "
                            "must appear in the plan"
                        ),
                    }
                )
                continue
            try:
                proposed_pct = Decimal(str(match.get("allocation_pct")))
            except (ArithmeticError, TypeError, ValueError):
                continue  # already reported above as shape violation
            if abs(proposed_pct - ov.allocation_pct) > _ALLOCATION_PCT_TOLERANCE:
                violations.append(
                    {
                        "kind": "override_drift",
                        "detail": (
                            f"override '{target_platform}' allocation_pct={proposed_pct} "
                            f"differs from required {ov.allocation_pct}"
                        ),
                    }
                )
            if not match.get("human_override"):
                violations.append(
                    {
                        "kind": "override_unflagged",
                        "detail": (
                            f"channel '{target_platform}' is an override but "
                            "human_override flag is not true"
                        ),
                    }
                )

        # KPIs — primary metric + rationale required.
        kpis = payload.get("kpis")
        if not isinstance(kpis, dict):
            violations.append({"kind": "shape", "detail": "kpis must be an object"})
        else:
            primary = kpis.get("primary")
            if not isinstance(primary, dict):
                violations.append({"kind": "shape", "detail": "kpis.primary must be an object"})
            else:
                if not str(primary.get("metric", "")).strip():
                    violations.append(
                        {"kind": "shape", "detail": "kpis.primary.metric is required"}
                    )
                if primary.get("target") is None:
                    violations.append(
                        {"kind": "shape", "detail": "kpis.primary.target is required"}
                    )
                if not str(primary.get("rationale", "")).strip():
                    violations.append(
                        {
                            "kind": "missing_rationale",
                            "detail": "kpis.primary.rationale is empty",
                        }
                    )

        return violations

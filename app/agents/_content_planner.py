"""Pure helpers the Content Creator uses to translate a touchpoint into a
prompt + asset row (W22, E06-S01/04).

No DB, no LLM. Just mapping tables, prompt assembly, and metadata bundling so
the agent module stays focused on persistence + transition logic and the
helpers stay easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.enums import AssetType, ChannelPlatform

# A touch on a given channel → the kind of asset we generate for it.
# This is the only place the mapping lives; the agent and tests both read here.
_PLATFORM_TO_ASSET_TYPE: dict[str, AssetType] = {
    ChannelPlatform.email.value: AssetType.email,
    ChannelPlatform.linkedin.value: AssetType.social_post,
    ChannelPlatform.x.value: AssetType.social_post,
    ChannelPlatform.meta.value: AssetType.social_post,
    ChannelPlatform.instagram.value: AssetType.social_post,
    ChannelPlatform.google_ads.value: AssetType.ad_creative,
    ChannelPlatform.meta_ads.value: AssetType.ad_creative,
    ChannelPlatform.web.value: AssetType.landing_page_copy,
    ChannelPlatform.blog.value: AssetType.blog_post,
    ChannelPlatform.sms.value: AssetType.sms,
}

# For each AssetType, which `channel` string we pass to the CopywritingTool.
# Most are 1:1 with the asset type; social_post and ad_creative fan out per
# platform so the tool can apply channel-specific length caps (e.g. x = 280).
_ASSET_TYPE_TO_TOOL_CHANNEL: dict[tuple[AssetType, str | None], str] = {
    (AssetType.email, None): "email",
    (AssetType.social_post, ChannelPlatform.linkedin.value): "linkedin",
    (AssetType.social_post, ChannelPlatform.x.value): "x",
    (AssetType.social_post, ChannelPlatform.meta.value): "social_post",
    (AssetType.social_post, ChannelPlatform.instagram.value): "social_post",
    (AssetType.ad_creative, ChannelPlatform.google_ads.value): "ad_creative",
    (AssetType.ad_creative, ChannelPlatform.meta_ads.value): "ad_creative",
    (AssetType.blog_post, None): "blog_post",
    (AssetType.landing_page_copy, None): "landing_page_copy",
    (AssetType.sms, None): "sms",
}

# Asset types that get an SEO pass on generation (E06-S03 minimal).
_SEO_ASSET_TYPES: frozenset[AssetType] = frozenset(
    {AssetType.blog_post, AssetType.landing_page_copy}
)


class PlannerError(Exception):
    """Raised on an unknown channel platform — we never want to silently
    generate an asset for a platform we don't know how to constrain."""


@dataclass(frozen=True)
class AssetPlan:
    """What the agent will actually send to the copywriting tool, plus the
    book-keeping it'll write into the content_asset row.

    `tool_channel` is the string the CopywritingTool expects in its `channel`
    field. `requires_seo` is true for long-form types so the agent runs the
    SEO tool with the campaign's target_keywords after generation."""

    asset_type: AssetType
    tool_channel: str
    requires_seo: bool


def plan_for_platform(platform: str) -> AssetPlan:
    """Resolve `(asset_type, tool_channel, requires_seo)` for a given channel
    platform. Raises `PlannerError` on an unmapped platform so the caller can
    surface a clean "regenerate after manual fix" error."""
    normalised = (platform or "").strip().lower()
    if normalised not in _PLATFORM_TO_ASSET_TYPE:
        raise PlannerError(f"no asset_type mapping for channel platform '{platform}'")

    asset_type = _PLATFORM_TO_ASSET_TYPE[normalised]
    tool_channel = _ASSET_TYPE_TO_TOOL_CHANNEL.get(
        (asset_type, normalised),
        _ASSET_TYPE_TO_TOOL_CHANNEL.get((asset_type, None), normalised),
    )
    return AssetPlan(
        asset_type=asset_type,
        tool_channel=tool_channel,
        requires_seo=asset_type in _SEO_ASSET_TYPES,
    )


def build_copywriting_inputs(
    *,
    plan: AssetPlan,
    campaign_brief: str | None,
    campaign_objective: str,
    audience_summary: str,
    voice_prompt: str | None,
    touchpoint_position: int,
    total_touchpoints_for_channel: int,
    target_keywords: list[str] | None = None,
    seed: str | int | None = None,
) -> dict[str, Any]:
    """Compose the kwargs sent to `CopywritingTool.call`.

    The agent merges the brand-voice prompt block into the `voice` field so
    the tool's existing prompt builder splices it in verbatim (E06-S02 AC #2);
    we don't duplicate that templating here."""
    parts: list[str] = []
    if campaign_brief:
        parts.append(campaign_brief.strip())
    parts.append(
        f"Touch {touchpoint_position} of {total_touchpoints_for_channel} on "
        f"the {plan.tool_channel} channel."
    )
    if plan.requires_seo and target_keywords:
        parts.append("Target keywords: " + ", ".join(target_keywords))

    inputs: dict[str, Any] = {
        "channel": plan.tool_channel,
        "asset_type": plan.asset_type.value,
        "brief": "\n\n".join(parts),
    }
    if voice_prompt:
        inputs["voice"] = voice_prompt
    if audience_summary:
        inputs["audience_summary"] = audience_summary
    if seed is not None:
        inputs["seed"] = seed
    return inputs


def extract_title(plan: AssetPlan, copywriting_output: dict[str, Any]) -> str | None:
    """Pick the user-facing title for the content_asset row from the tool's
    output, since the field name differs per channel (subject for email,
    headline for ad/landing, title for blog, first 80 chars of body otherwise)."""
    for field in ("title", "subject", "headline"):
        value = copywriting_output.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    body = copywriting_output.get("body")
    if isinstance(body, str) and body.strip():
        # social_post / sms — no explicit title field, fall back to a body prefix.
        return body.strip().splitlines()[0][:300]
    return None


def bundle_metadata(
    *,
    copywriting_output: dict[str, Any],
    brand_check: dict[str, Any],
    seo: dict[str, Any] | None,
    storage_uri: str | None = None,
) -> dict[str, Any]:
    """Pack the metadata blob written to `content_asset.metadata`."""
    fields = {
        k: v
        for k, v in copywriting_output.items()
        if k not in {"length_metrics", "length_warning", "body"}
        and isinstance(v, str)
    }
    bundle: dict[str, Any] = {
        "storage_uri": storage_uri,
        "fields": fields,
        "length_metrics": copywriting_output.get("length_metrics", {}),
        "brand_check": brand_check,
    }
    if "length_warning" in copywriting_output:
        bundle["length_warning"] = copywriting_output["length_warning"]
    if seo is not None:
        bundle["seo"] = seo
    return bundle


__all__ = [
    "AssetPlan",
    "PlannerError",
    "bundle_metadata",
    "build_copywriting_inputs",
    "extract_title",
    "plan_for_platform",
]

"""Jinja2 templates configuration for the UI surface (W32).

Provides a singleton `Templates` instance that the UI routers reuse, plus
small shared helpers (`status_badge_class`, etc.) that show up across
both campaign detail and approval review pages."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Status → CSS-class suffix mapping for the badge macro. Keeping this in
# Python lets us iterate without touching templates.
_STATUS_BADGE_VARIANTS: dict[str, str] = {
    # CampaignStatus
    "drafted": "neutral",
    "audience_built": "info",
    "strategy_set": "info",
    "content_in_production": "info",
    "approval_pending": "warn",
    "ready_to_launch": "info",
    "live": "success",
    "optimising": "success",
    "paused": "warn",
    "completed": "neutral",
    # AssetStatus
    "requested": "neutral",
    "generating": "info",
    "pending_approval": "warn",
    "approved": "success",
    "rejected": "danger",
    "scheduled": "info",
    "published": "success",
    "failed": "danger",
}


def status_badge_class(status: str) -> str:
    return _STATUS_BADGE_VARIANTS.get(status, "neutral")


# Expose helpers in the Jinja env so templates can call `status_badge_class('live')`.
templates.env.globals["status_badge_class"] = status_badge_class


__all__ = ["status_badge_class", "templates"]

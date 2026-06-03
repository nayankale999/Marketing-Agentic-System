"""Dashboard UI route + assistant chat endpoint (W42)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_role
from app.api.ui.templates import templates
from app.assistant.memory import (
    clear_history,
    get_active_campaign,
    load_history,
    save_history,
)
from app.assistant.router import AssistantError, handle_message
from app.dashboard.stats import load_dashboard_stats
from app.db.enums import UserRole
from app.db.models import AppUser, Campaign

router = APIRouter(prefix="/ui", tags=["ui"])


def _format_history_for_template(messages: list) -> list[dict]:
    """Convert stored Anthropic message dicts into a UI-friendly list of
    {role, text} bubbles. Tool-use + tool_result blocks collapse into a
    single 'tool ran' marker so the thread reads naturally."""
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            out.append({"role": "user", "text": content})
        elif role == "user" and isinstance(content, list):
            # tool_result block — show as a faded marker.
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                ):
                    out.append(
                        {
                            "role": "tool",
                            "text": (
                                str(block.get("content", ""))[:200]
                            ),
                        }
                    )
        elif role == "assistant" and isinstance(content, list):
            text_parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            tool_use = next(
                (
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ),
                None,
            )
            text = " ".join(t for t in text_parts if t).strip()
            if text or tool_use:
                out.append(
                    {
                        "role": "assistant",
                        "text": text,
                        "tool_name": tool_use.get("name") if tool_use else None,
                    }
                )
    return out


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """The dashboard — stats panel + assistant input + recent campaigns."""
    stats = await load_dashboard_stats(db)
    history = await load_history(db, user_id=current_user.id)
    thread = _format_history_for_template(history)
    active_campaign_id = await get_active_campaign(db, user_id=current_user.id)
    active_campaign = (
        await db.get(Campaign, active_campaign_id)
        if active_campaign_id is not None
        else None
    )
    if active_campaign is not None and active_campaign.tenant_id != current_user.tenant_id:
        active_campaign = None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "current_user": current_user,
            "thread": thread,
            "active_campaign": active_campaign,
        },
    )


@router.post("/assistant/chat", response_class=HTMLResponse)
async def assistant_chat(
    request: Request,
    message: Annotated[str, Form()],
    current_user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """HTMX endpoint — accepts the user's message, calls Claude with the
    tool catalog, returns HTML to splice into the response panel."""
    from anthropic import AsyncAnthropic
    from app.settings.config import get_settings

    settings = get_settings()
    user_message = message.strip()

    if not settings.anthropic_api_key:
        return templates.TemplateResponse(
            request,
            "_assistant_response.html",
            {
                "user_message": user_message,
                "result": _error_payload(
                    "ANTHROPIC_API_KEY is not configured. Ask your admin "
                    "to set it; the assistant needs a live LLM to route "
                    "intent.",
                ),
            },
            status_code=200,
        )

    history = await load_history(db, user_id=current_user.id)

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        result = await handle_message(
            session=db,
            user=current_user,
            message=user_message,
            history=history,
            client=client,
            model=settings.copywriting_model,  # reuse the same model setting
        )
    except AssistantError as exc:
        return templates.TemplateResponse(
            request,
            "_assistant_response.html",
            {
                "user_message": user_message,
                "result": _error_payload(str(exc)),
            },
            status_code=200,
        )

    # Persist the conversation so the next turn picks up where we left off.
    if result.messages:
        await save_history(
            db,
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            messages=result.messages,
        )

    return templates.TemplateResponse(
        request,
        "_assistant_response.html",
        {"user_message": user_message, "result": result},
    )


@router.post("/assistant/clear", response_class=HTMLResponse)
async def assistant_clear(
    request: Request,
    current_user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """Wipe the user's assistant memory + return an empty thread."""
    await clear_history(db, user_id=current_user.id)
    return HTMLResponse(content="", status_code=200)


def _error_payload(msg: str) -> object:
    """Minimal stand-in matching the AssistantResult shape the template
    expects (so error rendering goes through the same path)."""
    from app.assistant.router import AssistantResult

    return AssistantResult(text="", error=msg)


@router.get("/me", response_class=HTMLResponse)
async def ui_me(
    request: Request,
    current_user: AppUser = Depends(require_role(UserRole.viewer)),
    db: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """The 'My account' page — profile + recent activity + sign-out.
    Replaces the old redirect-to-dashboard placeholder."""
    from sqlalchemy import select as _select

    from app.db.models import (
        ApprovalDecisionLog,
        Campaign,
        ContentAsset,
        Tenant,
    )

    tenant = await db.get(Tenant, current_user.tenant_id)

    # Campaigns owned by this user (most recent first).
    my_campaigns = (
        await db.execute(
            _select(Campaign)
            .where(Campaign.owner_id == current_user.id)
            .order_by(Campaign.created_at.desc())
            .limit(5)
        )
    ).scalars().all()

    # Approval decisions this user made (most recent first).
    decisions_rows = (
        await db.execute(
            _select(ApprovalDecisionLog, ContentAsset, Campaign)
            .join(ContentAsset, ContentAsset.id == ApprovalDecisionLog.content_asset_id)
            .join(Campaign, Campaign.id == ContentAsset.campaign_id)
            .where(ApprovalDecisionLog.reviewer_id == current_user.id)
            .order_by(ApprovalDecisionLog.decided_at.desc())
            .limit(5)
        )
    ).all()
    my_decisions = [
        {
            "decision_id": d.id,
            "decision": d.decision.value,
            "decided_at": d.decided_at,
            "asset_id": a.id,
            "asset_title": a.title,
            "asset_type": a.asset_type.value,
            "campaign_id": c.id,
            "campaign_name": c.name,
            "reason": d.reason,
        }
        for d, a, c in decisions_rows
    ]

    return templates.TemplateResponse(
        request,
        "me.html",
        {
            "current_user": current_user,
            "tenant": tenant,
            "my_campaigns": my_campaigns,
            "my_decisions": my_decisions,
        },
    )

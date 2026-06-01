"""Dashboard UI route + assistant chat endpoint (W42)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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


@router.get("/me", include_in_schema=False)
async def ui_me_redirect(
    _user: AppUser = Depends(require_role(UserRole.viewer)),
) -> RedirectResponse:
    """Convenience: bare `/ui/me` redirects to the dashboard. The
    `/api/me` JSON endpoint stays where it is."""
    return RedirectResponse(url="/ui/")

"""Assistant router (W42) — Anthropic tool-use orchestrator.

Single turn flow:
  1. Take the user's message + conversation history.
  2. Call Claude with the registered tool catalog (see `tools.py`).
  3. If Claude returns a `tool_use` block, dispatch to the matching
     handler, capture its result, and either:
       - Reply with the result, OR
       - Loop back to Claude with the tool_result so the model can
         synthesise a natural-language reply.
  4. Bound the loop at 4 turns to avoid runaway costs / loops.

`handle_message` is the single public entry point. The chat endpoint
calls it; tests stub `client` to mock Anthropic responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.tools import (
    TOOL_DEFINITIONS,
    TOOL_HANDLERS,
    ToolError,
    ToolPermissionError,
    ToolResult,
)
from app.db.models import AppUser


# Cap how many round-trips one user message can take. Most flows are
# one or two: model decides tool, we run it, model summarises. Confirmation
# flows add one more turn. Beyond 4, we abort — usually it means the model
# is stuck in a loop.
_MAX_TURNS = 4


_SYSTEM_PROMPT = """\
You are the MAS (Marketing Agentic System) assistant. The user is a
marketer or marketing manager using the MAS dashboard.

Tone: direct, professional, helpful. Skip preambles like "I'd be happy
to help" — get to the answer. When you call a tool, the dashboard
renders the result in a structured panel; your text response should
add context the panel doesn't already convey (e.g. interpretation,
next steps).

Tool-use rules:
  * Pick exactly one tool per turn.
  * Destructive / expensive tools require confirmation: pause_campaign,
    complete_campaign, accept_recommendation, generate_strategy,
    generate_content, accept_strategy, approve_all_content,
    launch_campaign. On the first call, pass `confirm: false`. After the
    user explicitly confirms (e.g. "yes, confirm"), call again with
    `confirm: true`.
  * Never invent UUIDs. If you need one, use a tool to look it up first.

ASKING THE USER QUESTIONS:
  * For multiple-choice / enum-style questions, CALL `request_input`
    with `prompt` + `options`. Examples: campaign type, primary KPI
    metric, "synthetic audience vs CSV", "ready to draft strategy now?",
    yes/no follow-ups beyond the destructive-tool confirmation path.
  * Only use free-form text questions when the answer is genuinely
    open-ended (the campaign brief paragraphs, the campaign name, etc.).
  * NEVER list options in your text reply and ask the user to type one.
    Always use request_input.

THE END-TO-END CAMPAIGN FLOW:
After `create_campaign`, proactively walk the user through:
  1. `synthesise_audience` (or ask if they'd rather upload a CSV — but
     CSV upload via chat isn't implemented yet, so default to synthetic)
  2. `generate_strategy` (with confirmation — costs real Anthropic spend)
  3. `accept_strategy`
  4. `generate_content` (with confirmation)
  5. Review the drafts: by default, use `list_drafts_for_review` and let
     the user approve / reject each one. If the user says "approve all"
     or "looks good, ship it", use `approve_all_content` instead.
  6. `launch_campaign`

After each step, use `request_input` to ask if the user wants to proceed
to the next step. Don't lecture — be concise.

For `create_campaign`, all fields are required. If anything is missing,
use `request_input` to collect ENUM fields (campaign_type, primary KPI
metric) with chips. Use free-form text for name + brief + dates + budget.

PERSISTENT CAMPAIGN CONTEXT (active campaign pointer):
Every tool call that touches a campaign automatically updates the
"active campaign" pointer in the assistant's persistent state. So when
the user later says "approve it", "launch it now", or "what's left",
you can use the active campaign without asking again. If you genuinely
don't know which campaign is active, call `where_did_we_leave_off` —
it returns the last campaign + its state + the suggested next step.

If the user says "I'll come back later" or "approve it later", confirm
briefly (e.g. "OK — I'll remember we're on '[campaign name]'. Just say
'where did we leave off' when you're back.") and stop. Don't keep
pushing them through the flow.

OUTBOUND PERSONALISATION FLOW (CSV → enrichment → drafts):
When the user uploads a CSV of contacts (or mentions one is uploaded),
the natural sequence is:
  1. `enrich_audience` — fills missing fields via Apollo. No
     confirmation needed; Apollo credits are cheap. Run this before
     drafting so the LLM has titles + seniority to work with.
  2. `draft_outbound` — generates per-segment LinkedIn DM + email
     templates. REQUIRES CONFIRMATION (uses Anthropic credits). On
     first call pass `confirm: false`; the user sees the cost
     estimate (member count + LLM call count). After they say "yes,
     draft them", call again with `confirm: true`.
  3. `list_personalised_drafts` — show a preview of the rendered
     per-contact messages. Direct the user to
     /ui/campaigns/{id}/personalised-drafts for the full list with
     copy-to-clipboard.
The CSV itself is uploaded via the UI at /ui/outbound/upload —
direct the user there if they want to upload but haven't yet.

If a tool returns a permission error, explain to the user what role
they'd need and stop. Do not retry.

GROUND TRUTH RULE:
Earlier turns in this conversation are NOT authoritative — the
database is. If the user disagrees with something you said earlier
(e.g. "you said it's stuck but it's not"), or if a previous turn
said the campaign was in a particular status, CALL `get_campaign`
to re-check the live status before responding. Never tell the user
to "contact a platform admin" or that something is "blocked at the
backend" without first calling a tool to verify. The dashboard
backend has no manual override; if the system is wedged, the right
move is to call the relevant tool and surface the actual error."""


@dataclass
class AssistantResult:
    """Final payload the chat endpoint renders to HTML."""

    text: str
    tool_name: str | None = None
    tool_summary: str | None = None
    tool_data: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    error: str | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # W42.1: the full conversation including this turn's additions.
    # The chat endpoint persists this back to `assistant_conversation`
    # so the next turn picks up where we left off.
    messages: list[dict[str, Any]] = field(default_factory=list)


class AssistantError(Exception):
    """Raised when the assistant can't recover (model error, hit turn cap)."""


class ToolUnavailableError(AssistantError):
    """Model called a tool name we don't know about."""


async def handle_message(
    *,
    session: AsyncSession,
    user: AppUser,
    message: str,
    history: list[dict[str, Any]] | None = None,
    client: AsyncAnthropic,
    model: str,
) -> AssistantResult:
    """Process one user message through Claude + tools, return the
    payload the dashboard renders."""
    history = history or []
    messages: list[dict[str, Any]] = list(history) + [
        {"role": "user", "content": message}
    ]

    result = AssistantResult(text="")
    last_tool_result: ToolResult | None = None

    for turn in range(_MAX_TURNS):
        result.turns = turn + 1
        response: Message = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens += int(getattr(usage, "input_tokens", 0))
            result.output_tokens += int(getattr(usage, "output_tokens", 0))

        # Look for a tool_use block.
        tool_use: ToolUseBlock | None = None
        text_chunks: list[str] = []
        for block in response.content:
            if isinstance(block, ToolUseBlock):
                tool_use = block
                break
            if isinstance(block, TextBlock):
                text_chunks.append(block.text)

        if tool_use is None:
            # No tool call — model is just talking. Done.
            result.text = "\n\n".join(t for t in text_chunks if t.strip()).strip()
            if last_tool_result is not None:
                # Surface the most recent tool's data to the template even
                # though Claude is now narrating around it.
                result.tool_summary = last_tool_result.summary
                result.tool_data = last_tool_result.data
                result.requires_confirmation = last_tool_result.requires_confirmation
            from app.assistant.memory import serialize_blocks

            result.messages = list(messages) + [
                {
                    "role": "assistant",
                    "content": serialize_blocks(response.content),
                }
            ]
            return result

        # Dispatch the tool.
        handler = TOOL_HANDLERS.get(tool_use.name)
        if handler is None:
            raise ToolUnavailableError(
                f"Model called unknown tool '{tool_use.name}'."
            )

        try:
            tool_input = dict(tool_use.input or {})
            tool_result = await handler(session, user=user, **tool_input)
            last_tool_result = tool_result
            result.tool_name = tool_use.name
            tool_text = tool_result.summary
            is_error = False
            # W42.3: persistent campaign focus. Whenever a tool returns a
            # campaign_id, treat it as "the user is now working on this
            # campaign" so later turns can resolve "approve it" / "launch
            # it" even after the message window trims.
            campaign_id_str = (tool_result.data or {}).get("campaign_id")
            if campaign_id_str:
                from uuid import UUID as _UUID

                from app.assistant.memory import set_active_campaign

                try:
                    cid = _UUID(str(campaign_id_str))
                    await set_active_campaign(
                        session,
                        user_id=user.id,
                        tenant_id=user.tenant_id,
                        campaign_id=cid,
                    )
                except (ValueError, TypeError):
                    pass
        except ToolPermissionError as exc:
            tool_text = f"PERMISSION_ERROR: {exc}"
            tool_result = ToolResult(summary=str(exc))
            last_tool_result = tool_result
            is_error = True
        except ToolError as exc:
            tool_text = f"ERROR: {exc}"
            tool_result = ToolResult(summary=str(exc))
            last_tool_result = tool_result
            is_error = True
        except Exception as exc:
            tool_text = f"INTERNAL_ERROR: {exc!s}"
            tool_result = ToolResult(summary=f"Internal error: {exc!s}")
            last_tool_result = tool_result
            is_error = True

        # If the tool requires confirmation, short-circuit — don't go
        # back to Claude. The user needs to respond next.
        if tool_result.requires_confirmation:
            result.text = "\n\n".join(t for t in text_chunks if t.strip()).strip()
            result.tool_summary = tool_result.summary
            result.tool_data = tool_result.data
            result.requires_confirmation = True
            from app.assistant.memory import serialize_blocks

            result.messages = list(messages) + [
                {
                    "role": "assistant",
                    "content": serialize_blocks(response.content),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": tool_text,
                            "is_error": is_error,
                        }
                    ],
                },
            ]
            return result

        # Feed the tool_result back to Claude so it can synthesise a reply.
        from app.assistant.memory import serialize_blocks

        messages = messages + [
            {"role": "assistant", "content": serialize_blocks(response.content)},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": tool_text,
                        "is_error": is_error,
                    }
                ],
            },
        ]

    # Hit the turn cap.
    raise AssistantError(
        f"Assistant exceeded {_MAX_TURNS} turns without a final response."
    )


__all__ = [
    "AssistantError",
    "AssistantResult",
    "ToolUnavailableError",
    "handle_message",
]

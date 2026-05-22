"""`social.publish` tool (W30, E11-S05).

Cross-platform concerns the agent shouldn't re-implement per provider:

  * Idempotency — `idempotency_key` is the dedup anchor. We check
    `dispatch_attempt` for an existing `sent` row with the same key
    BEFORE calling the platform; if present, we return the cached
    `provider_post_id` without a second publish (AC #3).
  * Media-required validation — if the caller flagged `media_required`
    but didn't supply a `media_url`, we raise `MediaRequiredError`
    pre-call so the operator sees a precondition failure rather than a
    platform-side rejection (AC #4).
  * Normalised result — `{provider_post_id, url, status}` regardless of
    provider (AC #1).

Retry-on-retryable-codes (AC #2) lives in the LinkedInConnector itself
because each provider has its own status code conventions. The tool
surfaces transport failures cleanly so the queue's outer retry takes
over without flipping the asset off `scheduled`.

Like W27's EmailDispatchTool, this is NOT registered in the global tool
registry — it needs per-call DB + connector. The W30 dispatch handler
constructs it per-task.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DispatchAttempt
from app.integrations.social.base import (
    MediaRequiredError,
    OAuthRevokedError,
    PostResult,
    ProviderRejectedError,
    ProviderUnreachableError,
    SocialConnector,
    SocialPost,
)
from app.tools.base import Tool


class SocialPublishError(Exception):
    """Tool-level error — bad inputs, missing channel record, etc."""


class SocialPublishTool(Tool):
    name: ClassVar[str] = "social.publish"
    description: ClassVar[str] = (
        "Publish a post via the tenant's configured social provider with "
        "idempotency, media validation, and a normalized result shape."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "platform": {"type": "string"},
            "page_urn": {"type": "string"},
            "content": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "media_url": {"type": "string"},
                    "media_required": {"type": "boolean"},
                    "visibility": {"type": "string"},
                },
                "required": ["text"],
            },
            "idempotency_key": {"type": "string"},
        },
        "required": ["platform", "page_urn", "content", "idempotency_key"],
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "provider_post_id": {"type": "string"},
            "url": {"type": "string"},
            "status": {"type": "string"},
            "idempotent_hit": {"type": "boolean"},
        },
        "required": ["provider_post_id", "url", "status"],
    }

    def __init__(
        self,
        *,
        connector: SocialConnector,
        access_token: str,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> None:
        self._connector = connector
        self._access_token = access_token
        self._session = session
        self._tenant_id = tenant_id

    async def call(self, inputs: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(inputs.get("idempotency_key", "")).strip()
        if not idempotency_key:
            raise SocialPublishError("idempotency_key is required")

        platform = str(inputs.get("platform", "")).strip().lower()
        if platform != self._connector.provider:
            raise SocialPublishError(
                f"connector provider '{self._connector.provider}' does not match "
                f"requested platform '{platform}'"
            )

        page_urn = str(inputs.get("page_urn", "")).strip()
        if not page_urn:
            raise SocialPublishError("page_urn is required")

        content = inputs.get("content") or {}
        text = str(content.get("text", "")).strip()
        if not text:
            raise SocialPublishError("content.text is required")

        # AC #3: same idempotency_key → return cached provider_post_id.
        cached = await self._cached_attempt(idempotency_key)
        if cached is not None and cached.status == "sent" and cached.provider_message_id:
            return {
                "provider_post_id": cached.provider_message_id,
                "url": _reconstruct_url(self._connector.provider, cached.provider_message_id),
                "status": "published",
                "idempotent_hit": True,
            }

        post = SocialPost(
            text=text,
            media_url=content.get("media_url"),
            media_required=bool(content.get("media_required", False)),
            visibility=str(content.get("visibility") or "PUBLIC"),
        )

        # MediaRequiredError surfaces pre-call (AC #4).
        # OAuthRevokedError / ProviderUnreachableError bubble up to the
        # dispatch handler so it can pause the campaign or let the queue
        # retry. ProviderRejectedError is a 4xx we re-raise as-is.
        result: PostResult = await self._connector.publish_post(
            access_token=self._access_token,
            page_urn=page_urn,
            post=post,
        )

        return {
            "provider_post_id": result.provider_post_id,
            "url": result.url,
            "status": "published",
            "idempotent_hit": False,
        }

    async def _cached_attempt(
        self, idempotency_key: str
    ) -> DispatchAttempt | None:
        return (
            await self._session.execute(
                select(DispatchAttempt).where(
                    DispatchAttempt.tenant_id == self._tenant_id,
                    DispatchAttempt.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()


def _reconstruct_url(provider: str, provider_post_id: str) -> str:
    if provider == "linkedin":
        return f"https://www.linkedin.com/feed/update/{provider_post_id}/"
    return provider_post_id


__all__ = [
    "MediaRequiredError",
    "OAuthRevokedError",
    "ProviderRejectedError",
    "ProviderUnreachableError",
    "SocialPublishError",
    "SocialPublishTool",
]

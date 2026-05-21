"""Provider-agnostic CRM connector interface.

HubSpot is the first implementation (W11). Salesforce / Dynamics 365 implement
this same interface in later work units. The orchestrator + API layer talk
only to the abstract class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CrmRecord:
    """Normalised representation of a CRM contact or company."""

    external_id: str
    properties: dict[str, Any]
    updated_at: datetime | None = None


class CrmConnector(ABC):
    """One concrete subclass per CRM provider."""

    provider: ClassVar[str]
    default_scopes: ClassVar[tuple[str, ...]]

    @abstractmethod
    def authorize_url(
        self, *, state: str, redirect_uri: str, scopes: list[str] | None = None
    ) -> str: ...

    @abstractmethod
    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthTokens: ...

    @abstractmethod
    async def refresh(self, *, refresh_token: str) -> OAuthTokens: ...

    @abstractmethod
    async def list_contacts(
        self,
        *,
        access_token: str,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[list[CrmRecord], str | None]:
        """Return one page of contacts + the cursor for the next page (or None)."""

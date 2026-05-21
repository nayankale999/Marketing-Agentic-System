"""Provider-agnostic web-analytics connector interface.

Plausible lands first (W16). GA4 implements the same interface when its
work unit arrives. The ingest writer (`app/integrations/web_analytics/
ingest.py`) talks only to this base class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from app.db.enums import EventKind


@dataclass(frozen=True)
class WebAnalyticsEvent:
    """One aggregated metric the connector wants ingested.

    `provider_event_id` is the connector's dedup key (e.g.
    'plausible:acme.com:2026-05-20:visitors:spring-launch'). The ingest
    writer reuses it via ON CONFLICT DO NOTHING.
    """

    provider_event_id: str
    event_type: EventKind
    metric_value: float
    event_at: datetime
    utm_campaign: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class WebAnalyticsConnector(ABC):
    provider: ClassVar[str]

    @abstractmethod
    async def fetch_events(self, *, since: datetime, until: datetime) -> list[WebAnalyticsEvent]:
        """Pull aggregated metrics from the provider for the [since, until] window."""

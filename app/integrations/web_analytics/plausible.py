"""Plausible Analytics connector (W16, E12-S05).

We hit Plausible's `breakdown` endpoint with `property=visit:utm_campaign` so
each result row is one (utm_campaign, period) bucket. The connector emits a
`WebAnalyticsEvent` per row; the ingest writer dedups + attributes.

Real production usage will run this on a 15-minute schedule via the
orchestrator task `ingest.web_analytics.plausible`. For W16 we expose a
manual sync via `POST /api/integrations/plausible/sync`; the scheduled
variant comes later.
"""

from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from app.db.enums import EventKind
from app.integrations.web_analytics.base import (
    WebAnalyticsConnector,
    WebAnalyticsEvent,
)

_HTTP_TIMEOUT = httpx.Timeout(10.0)


class PlausibleConnector(WebAnalyticsConnector):
    provider: ClassVar[str] = "plausible"
    BREAKDOWN_URL: ClassVar[str] = "https://plausible.io/api/v1/stats/breakdown"

    def __init__(self, *, api_key: str, site_id: str) -> None:
        self._api_key = api_key
        self._site_id = site_id

    async def fetch_events(self, *, since: datetime, until: datetime) -> list[WebAnalyticsEvent]:
        """Pull a utm_campaign breakdown for the window. Each row -> one event.

        Plausible accepts `period=custom&date=YYYY-MM-DD,YYYY-MM-DD`.
        """
        params = {
            "site_id": self._site_id,
            "period": "custom",
            "date": f"{since.date().isoformat()},{until.date().isoformat()}",
            "property": "visit:utm_campaign",
            "metrics": "visitors,pageviews",
        }
        url = f"{self.BREAKDOWN_URL}?{urlencode(params)}"

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {self._api_key}"})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        events: list[WebAnalyticsEvent] = []
        for row in data.get("results", []):
            utm = row.get("visit:utm_campaign") or row.get("utm_campaign")
            pageviews = row.get("pageviews") or 0
            visitors = row.get("visitors") or 0
            window_id = f"{since.date().isoformat()}:{until.date().isoformat()}"
            base_id = f"plausible:{self._site_id}:{window_id}:{utm or 'none'}"
            payload = {"utm_campaign": utm, "raw": row}

            # Emit two events per bucket: impression (pageviews) + click-ish
            # proxy (visitors). Mapping to EventKind is best-effort; refine
            # when the analytics agent's metric model is wired up.
            events.append(
                WebAnalyticsEvent(
                    provider_event_id=f"{base_id}:pageviews",
                    event_type=EventKind.impression,
                    metric_value=float(pageviews),
                    event_at=until,
                    utm_campaign=utm,
                    payload=payload,
                )
            )
            events.append(
                WebAnalyticsEvent(
                    provider_event_id=f"{base_id}:visitors",
                    event_type=EventKind.click,
                    metric_value=float(visitors),
                    event_at=until,
                    utm_campaign=utm,
                    payload=payload,
                )
            )

        return events

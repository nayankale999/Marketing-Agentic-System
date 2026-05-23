"""Pydantic schemas for the campaign KPI dashboard endpoint (W34, E10-S01)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class KpiDerived(BaseModel):
    ctr: str
    open_rate: str
    click_to_open: str
    unsubscribe_rate: str
    cpl: str | None
    cpa: str | None


class CampaignKpisOut(BaseModel):
    impressions: int
    opens: int
    clicks: int
    replies: int
    conversions: int
    unsubscribes: int
    bounces: int
    spam_complaints: int
    spend: str
    derived: KpiDerived


class SourceFreshnessOut(BaseModel):
    name: str
    last_event_at: datetime | None
    freshness_seconds: int | None
    documented_latency_seconds: int


class CampaignKpiSnapshotOut(BaseModel):
    campaign_id: UUID
    kpis: CampaignKpisOut
    sources: list[SourceFreshnessOut]
    generated_at: datetime

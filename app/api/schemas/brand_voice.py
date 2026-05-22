"""Pydantic schemas for the /api/brand-voices surface (E06-S02)."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BrandVoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)]
    is_active: bool = False
    tone_descriptors: list[str] = Field(default_factory=list)
    do_words: list[str] = Field(default_factory=list)
    dont_words: list[str] = Field(default_factory=list)
    sample_paragraphs: list[str] = Field(default_factory=list)
    reading_grade_target: Annotated[
        Decimal | None, Field(default=None, ge=Decimal("1.0"), le=Decimal("20.0"))
    ] = None


class BrandVoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=100)] = None
    tone_descriptors: list[str] | None = None
    do_words: list[str] | None = None
    dont_words: list[str] | None = None
    sample_paragraphs: list[str] | None = None
    reading_grade_target: Annotated[
        Decimal | None, Field(default=None, ge=Decimal("1.0"), le=Decimal("20.0"))
    ] = None


class BrandVoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    is_active: bool
    tone_descriptors: list[str]
    do_words: list[str]
    dont_words: list[str]
    sample_paragraphs: list[str]
    reading_grade_target: Decimal | None
    created_at: datetime
    updated_at: datetime


class BrandVoiceListResponse(BaseModel):
    items: list[BrandVoiceOut]
    total: int

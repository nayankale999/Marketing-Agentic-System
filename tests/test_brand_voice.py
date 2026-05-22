"""W19 — Brand voice configuration (E06-S02).

Two layers under test:

  * `app.agents.brand_voice` helpers — pure functions, no DB.
  * `/api/brand-voices` router — CRUD, activation toggle, RLS, role gating.

The activate path is the one with concurrency teeth (partial unique index on
`(tenant_id) WHERE is_active`), so we exercise it with overlapping calls and
confirm only one row survives as active.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agents.brand_voice import check_dont_words, format_brand_voice_prompt
from app.api.app import app
from app.api.deps import get_current_user
from app.db.enums import UserRole
from app.db.models import AppUser, AuditLog, BrandVoice, Tenant


# ---------------------------------------------------------------------------
# Helper unit tests (no DB)
# ---------------------------------------------------------------------------


@dataclass
class _Voice:
    name: str = "default"
    tone_descriptors: list[str] = field(default_factory=list)
    do_words: list[str] = field(default_factory=list)
    dont_words: list[str] = field(default_factory=list)
    sample_paragraphs: list[str] = field(default_factory=list)
    reading_grade_target: object = None


def test_formatter_emits_every_configured_field_verbatim() -> None:
    voice = _Voice(
        name="B2B Crisp",
        tone_descriptors=["confident", "concise", "human"],
        do_words=["partner", "platform"],
        dont_words=["disrupt", "synergy"],
        sample_paragraphs=["We ship every Friday.", "No vapor, just receipts."],
        reading_grade_target=Decimal("8.0"),
    )
    out = format_brand_voice_prompt(voice)

    assert "## Brand voice: B2B Crisp" in out
    assert "confident, concise, human" in out
    assert "partner, platform" in out
    assert "disrupt, synergy" in out
    assert "Target reading grade level: 8.0" in out
    assert "[1] We ship every Friday." in out
    assert "[2] No vapor, just receipts." in out


def test_formatter_omits_unset_sections() -> None:
    voice = _Voice(
        name="minimal",
        tone_descriptors=["sharp"],
    )
    out = format_brand_voice_prompt(voice)

    assert "## Brand voice: minimal" in out
    assert "Tone descriptors: sharp" in out
    assert "Prefer these" not in out
    assert "Avoid these" not in out
    assert "Sample paragraphs" not in out
    assert "reading grade level" not in out


def test_formatter_is_deterministic() -> None:
    voice = _Voice(
        name="x",
        tone_descriptors=["a", "b"],
        do_words=["one"],
        dont_words=["two"],
        sample_paragraphs=["p1"],
        reading_grade_target=Decimal("7.5"),
    )
    assert format_brand_voice_prompt(voice) == format_brand_voice_prompt(voice)


def test_check_dont_words_matches_case_insensitively() -> None:
    text = "We need to Disrupt the market with synergy."
    hits = check_dont_words(text, ["disrupt", "synergy"])
    assert hits == ["disrupt", "synergy"]


def test_check_dont_words_ignores_substring_collisions() -> None:
    # "synergy" must not match inside "energy"; "win" must not match "winding".
    text = "Use energy, follow the winding path."
    assert check_dont_words(text, ["synergy", "win"]) == []


def test_check_dont_words_supports_multi_word_phrases() -> None:
    text = "We're best in class, period."
    assert check_dont_words(text, ["best in class", "world-class"]) == ["best in class"]


def test_check_dont_words_preserves_input_order() -> None:
    text = "zeta alpha beta"
    assert check_dont_words(text, ["zeta", "alpha", "beta"]) == ["zeta", "alpha", "beta"]
    assert check_dont_words(text, ["beta", "alpha"]) == ["beta", "alpha"]


def test_check_dont_words_handles_empty_inputs() -> None:
    assert check_dont_words("", ["x"]) == []
    assert check_dont_words("anything", []) == []
    assert check_dont_words("anything", ["", "   "]) == []


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def tenant_in_db(db_engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        tenant = Tenant(name=f"voice-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _make_user(
    engine: AsyncEngine, tenant_id: uuid.UUID, role: UserRole
) -> AppUser:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        user = AppUser(
            tenant_id=tenant_id,
            email=f"{role.value}-{uuid.uuid4().hex[:6]}@voice.test",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def client_as(
    override_api_db,
    db_engine: AsyncEngine,
    tenant_in_db: uuid.UUID,
) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []

    async def _factory(
        role: UserRole, *, tenant_id: uuid.UUID | None = None
    ) -> tuple[httpx.AsyncClient, AppUser]:
        user = await _make_user(db_engine, tenant_id or tenant_in_db, role)
        app.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client, user

    try:
        yield _factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_current_user, None)


def _create_body(**overrides: object) -> dict:
    base = {
        "name": f"voice-{uuid.uuid4().hex[:6]}",
        "is_active": False,
        "tone_descriptors": ["confident", "concise"],
        "do_words": ["partner"],
        "dont_words": ["disrupt"],
        "sample_paragraphs": ["A sample paragraph."],
        "reading_grade_target": "8.0",
    }
    base.update(overrides)
    return base


# ---- Create ---------------------------------------------------------------


async def test_create_voice_returns_201_with_shape(client_as) -> None:
    client, user = await client_as(UserRole.marketer)
    resp = await client.post("/api/brand-voices", json=_create_body(name="B2B"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "B2B"
    assert body["tenant_id"] == str(user.tenant_id)
    assert body["tone_descriptors"] == ["confident", "concise"]
    assert body["dont_words"] == ["disrupt"]
    assert body["reading_grade_target"] == "8.00"
    assert body["is_active"] is False


async def test_create_active_voice_makes_it_the_only_active(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)

    r1 = await client.post("/api/brand-voices", json=_create_body(name="v1", is_active=True))
    assert r1.status_code == 201
    r2 = await client.post("/api/brand-voices", json=_create_body(name="v2", is_active=True))
    assert r2.status_code == 201

    active = await client.get("/api/brand-voices/active")
    assert active.status_code == 200
    assert active.json()["name"] == "v2"


async def test_create_duplicate_name_returns_409(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    r1 = await client.post("/api/brand-voices", json=_create_body(name="same"))
    assert r1.status_code == 201
    r2 = await client.post("/api/brand-voices", json=_create_body(name="same"))
    assert r2.status_code == 409


async def test_viewer_cannot_create(client_as) -> None:
    client, _ = await client_as(UserRole.viewer)
    resp = await client.post("/api/brand-voices", json=_create_body())
    assert resp.status_code == 403


# ---- List / Get ------------------------------------------------------------


async def test_list_returns_active_first(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post("/api/brand-voices", json=_create_body(name="aaa"))
    await client.post("/api/brand-voices", json=_create_body(name="bbb", is_active=True))

    resp = await client.get("/api/brand-voices")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [v["name"] for v in items] == ["bbb", "aaa"]
    assert items[0]["is_active"] is True


async def test_active_endpoint_returns_404_when_none_active(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    await client.post("/api/brand-voices", json=_create_body(name="inactive-only"))
    resp = await client.get("/api/brand-voices/active")
    assert resp.status_code == 404


# ---- Patch ----------------------------------------------------------------


async def test_patch_updates_fields_and_writes_audit(
    client_as, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.marketer)
    created = (await client.post("/api/brand-voices", json=_create_body(name="orig"))).json()

    resp = await client.patch(
        f"/api/brand-voices/{created['id']}",
        json={"tone_descriptors": ["bold"], "do_words": ["ship"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tone_descriptors"] == ["bold"]
    assert body["do_words"] == ["ship"]
    # dont_words untouched
    assert body["dont_words"] == ["disrupt"]

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "brand_voice",
                    AuditLog.entity_id == uuid.UUID(created["id"]),
                    AuditLog.action == "updated",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert set(audits[0].extra_metadata["fields"]) == {"tone_descriptors", "do_words"}


# ---- Activate -------------------------------------------------------------


async def test_activate_flips_other_voices_off(
    client_as, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.marketer)
    v1 = (await client.post("/api/brand-voices", json=_create_body(name="v1", is_active=True))).json()
    v2 = (await client.post("/api/brand-voices", json=_create_body(name="v2"))).json()

    resp = await client.post(f"/api/brand-voices/{v2['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(BrandVoice.id, BrandVoice.is_active).order_by(BrandVoice.name)
            )
        ).all()
        active = {r.id: r.is_active for r in rows}
        assert active[uuid.UUID(v1["id"])] is False
        assert active[uuid.UUID(v2["id"])] is True


async def test_activate_writes_audit_with_activated_action(
    client_as, db_engine: AsyncEngine
) -> None:
    client, _ = await client_as(UserRole.marketer)
    v = (await client.post("/api/brand-voices", json=_create_body(name="v"))).json()
    resp = await client.post(f"/api/brand-voices/{v['id']}/activate")
    assert resp.status_code == 200

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "brand_voice",
                    AuditLog.entity_id == uuid.UUID(v["id"]),
                    AuditLog.action == "activated",
                )
            )
        ).scalars().all()
        assert len(audits) == 1


async def test_activate_is_idempotent(client_as) -> None:
    client, _ = await client_as(UserRole.marketer)
    v = (await client.post("/api/brand-voices", json=_create_body(name="v", is_active=True))).json()
    resp = await client.post(f"/api/brand-voices/{v['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# ---- RLS / cross-tenant isolation -----------------------------------------


async def test_voice_from_other_tenant_is_invisible(
    client_as, db_engine: AsyncEngine
) -> None:
    # Tenant A creates a voice
    client_a, user_a = await client_as(UserRole.marketer)
    created = (await client_a.post("/api/brand-voices", json=_create_body(name="A-only"))).json()

    # Tenant B (fresh tenant) can't see it
    async with AsyncSession(db_engine, expire_on_commit=False) as session, session.begin():
        other = Tenant(name=f"other-{uuid.uuid4().hex[:6]}")
        session.add(other)
        await session.flush()
        other_id = other.id

    client_b, _ = await client_as(UserRole.marketer, tenant_id=other_id)
    list_resp = await client_b.get("/api/brand-voices")
    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []

    detail_resp = await client_b.get(f"/api/brand-voices/{created['id']}")
    assert detail_resp.status_code == 404

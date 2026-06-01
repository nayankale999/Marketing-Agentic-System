"""OIDC authentication endpoints (authlib) + JIT user provisioning.

Login flow:
  1. /api/auth/login  -> redirect to OIDC provider's authorization endpoint
  2. provider redirects to /api/auth/callback with `code`
  3. callback exchanges code for tokens, verifies ID token, JIT-provisions the
     user under the tenant matching the Google `hd` (hosted domain) claim, and
     sets `session["user_id"]` + `session["tenant_id"]`
  4. /api/auth/logout clears the session
"""

from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.enums import UserRole
from app.db.models import AppUser, Tenant
from app.settings.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _build_oauth() -> OAuth:
    s = get_settings()
    oauth = OAuth()
    oauth.register(
        name="oidc",
        client_id=s.oidc_client_id,
        client_secret=s.oidc_client_secret,
        server_metadata_url=f"{s.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


_oauth = _build_oauth()


@router.get("/login", include_in_schema=False)
async def login(request: Request) -> RedirectResponse:
    # authlib lacks type stubs; treat the response as RedirectResponse.
    return await _oauth.oidc.authorize_redirect(  # type: ignore[no-any-return]
        request, get_settings().oidc_redirect_uri
    )


@router.get("/callback", include_in_schema=False)
async def callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    try:
        token = await _oauth.oidc.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"OIDC error: {exc}") from exc

    claims: dict[str, Any] = token.get("userinfo") or {}
    email = claims.get("email")
    hd = claims.get("hd")
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="ID token missing email")
    if not hd:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="ID token missing hosted-domain claim (hd)",
        )

    tenant = (
        await db.execute(select(Tenant).where(Tenant.oidc_hosted_domain == hd))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"No tenant configured for hosted domain '{hd}'",
        )

    user = (
        await db.execute(
            select(AppUser).where(AppUser.tenant_id == tenant.id, AppUser.email == email)
        )
    ).scalar_one_or_none()
    if user is None:
        user = AppUser(
            tenant_id=tenant.id,
            email=email,
            display_name=claims.get("name") or email,
            role=UserRole.viewer,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    request.session["user_id"] = str(user.id)
    request.session["tenant_id"] = str(tenant.id)
    return RedirectResponse(url="/api/me")


@router.get("/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/")


@router.get("/dev-impersonate", include_in_schema=False)
async def dev_impersonate(
    request: Request,
    email: str,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Dev-only: sets a session cookie for the user with the given email
    without going through OIDC. Useful for browser walkthroughs when the
    OIDC mock config has drift. Guarded by `DEV_IMPERSONATION_ENABLED`
    so it can't slip into a non-dev deployment."""
    if not get_settings().dev_impersonation_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = (
        await db.execute(select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no user with email '{email}'",
        )
    request.session["user_id"] = str(user.id)
    request.session["tenant_id"] = str(user.tenant_id)
    return RedirectResponse(url="/api/me")

"""Test endpoints exercising role enforcement. Not part of the user-facing API."""

from fastapi import APIRouter, Depends

from app.api.deps import require_role
from app.db.enums import UserRole

router = APIRouter(
    prefix="/api/_protected",
    tags=["_internal"],
    include_in_schema=False,
)


@router.get("/marketer")
def marketer_only(_: object = Depends(require_role(UserRole.marketer))) -> dict[str, str]:
    return {"status": "ok"}

"""Current-user endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.db.models import AppUser

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
def me(current: AppUser = Depends(get_current_user)) -> dict[str, str | None]:
    return {
        "id": str(current.id),
        "tenant_id": str(current.tenant_id),
        "email": current.email,
        "display_name": current.display_name,
        "role": current.role.value,
    }

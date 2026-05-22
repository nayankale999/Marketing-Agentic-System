"""Server-rendered HTMX UI (W32, E13-S02 + E13-S03).

Mounted under `/ui/...` to stay clear of `/api/...`. Templates live in
`app/templates/`; static CSS in `app/static/`. Auth + role gating reuses
the existing `require_role` deps so the UI shares the same OIDC session
as the API.
"""

from app.api.ui import approvals, campaigns

__all__ = ["approvals", "campaigns"]

"""W3: every API route must declare a required role OR be on the allowlist.

CI breaks if a new route lands without `require_role(...)`. The allowlist is
intentionally narrow: only public endpoints and the OIDC auth flow itself.
"""

from fastapi.routing import APIRoute

from app.api.app import app

# Public / signed-in-without-role-required endpoints.
ALLOWLIST: set[str] = {
    "/health",
    "/api/me",
    "/api/auth/login",
    "/api/auth/callback",
    "/api/auth/logout",
}


def _route_has_role_dep(route: APIRoute) -> bool:
    """DFS the route's dependency tree for the require_role marker."""
    if route.dependant is None:
        return False

    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        if getattr(dep.call, "__role_dep__", False):
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_route_has_role_or_is_allowlisted() -> None:
    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # mounts, openapi-internal, etc.
        if route.path in ALLOWLIST:
            continue
        if not _route_has_role_dep(route):
            missing.append(route.path)
    assert missing == [], f"routes missing require_role dependency: {missing}"

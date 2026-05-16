"""HTTP middleware that decorates the active OTel span with tenant + user ids.

FastAPIInstrumentor already produces a span per request; this middleware just
adds attributes once we know who the request is for.
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from opentelemetry import trace


async def tag_span_with_actor(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        try:
            session_data = request.session
        except (AssertionError, AttributeError):
            session_data = None
        if session_data:
            user_id = session_data.get("user_id")
            tenant_id = session_data.get("tenant_id")
            if user_id:
                span.set_attribute("mas.user.id", str(user_id))
            if tenant_id:
                span.set_attribute("mas.tenant.id", str(tenant_id))
    return await call_next(request)

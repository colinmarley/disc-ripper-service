import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from .context import correlation_id_var, session_id_var

CORRELATION_HEADER = "X-Correlation-ID"
SESSION_COOKIE = "session_id"


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        session_id = request.cookies.get(SESSION_COOKIE, "")

        token_corr = correlation_id_var.set(correlation_id)
        token_sess = session_id_var.set(session_id)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            correlation_id_var.reset(token_corr)
            session_id_var.reset(token_sess)

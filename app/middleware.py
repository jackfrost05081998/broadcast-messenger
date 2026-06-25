"""HTTP middleware."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth import create_access_token, decode_access_token
from app.config import get_settings


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    """Extend the session cookie on each authenticated request (sliding login)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        settings = get_settings()
        token = request.cookies.get(settings.session_cookie_name)
        if not token:
            return response
        user_id = decode_access_token(token)
        if user_id is None:
            return response
        response.set_cookie(
            key=settings.session_cookie_name,
            value=create_access_token(user_id),
            max_age=settings.session_max_age,
            httponly=True,
            samesite="lax",
            secure=settings.app_url.startswith("https://"),
        )
        return response

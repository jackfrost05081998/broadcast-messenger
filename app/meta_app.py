"""Per-user Meta Developer app credentials (App ID + Secret)."""

from dataclasses import dataclass

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    get_settings,
    is_cloud_deployment,
    is_valid_facebook_app_id,
    is_valid_facebook_app_secret,
)
from app.models import User

PENDING_COOKIE = "pending_meta_app"
PENDING_MAX_AGE = 1800


@dataclass(frozen=True)
class MetaAppCredentials:
    app_id: str
    app_secret: str

    @property
    def configured(self) -> bool:
        return is_valid_facebook_app_id(self.app_id) and is_valid_facebook_app_secret(
            self.app_secret
        )


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="meta-app-pending")


def set_pending_meta_app(response: Response, app_id: str, app_secret: str) -> None:
    token = _serializer().dumps({"app_id": app_id, "app_secret": app_secret})
    response.set_cookie(
        key=PENDING_COOKIE,
        value=token,
        max_age=PENDING_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().app_url.startswith("https://"),
    )


def clear_pending_meta_app(response: Response) -> None:
    response.delete_cookie(PENDING_COOKIE)


def get_pending_meta_app(request: Request) -> MetaAppCredentials | None:
    raw = request.cookies.get(PENDING_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=PENDING_MAX_AGE)
        app_id = str(data.get("app_id", "")).strip()
        app_secret = str(data.get("app_secret", "")).strip()
        creds = MetaAppCredentials(app_id, app_secret)
        return creds if creds.configured else None
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def credentials_from_user(user: User | None) -> MetaAppCredentials | None:
    if not user or not user.meta_app_id or not user.meta_app_secret:
        return None
    creds = MetaAppCredentials(user.meta_app_id.strip(), user.meta_app_secret.strip())
    return creds if creds.configured else None


def credentials_from_env() -> MetaAppCredentials | None:
    settings = get_settings()
    if not settings.facebook_configured:
        return None
    return MetaAppCredentials(settings.facebook_app_id, settings.facebook_app_secret)


async def resolve_meta_credentials(
    request: Request,
    user: User | None,
) -> MetaAppCredentials | None:
    """User DB record → pending sign-in cookie → platform .env (local dev only)."""
    sources = [credentials_from_user(user), get_pending_meta_app(request)]
    if not is_cloud_deployment():
        sources.append(credentials_from_env())
    for source in sources:
        if source and source.configured:
            return source
    return None


def apply_user_meta_app(user: User, app_id: str, app_secret: str) -> None:
    user.meta_app_id = app_id
    user.meta_app_secret = app_secret


async def save_user_meta_app(
    db: AsyncSession, user: User, app_id: str, app_secret: str
) -> None:
    apply_user_meta_app(user, app_id, app_secret)
    await db.commit()

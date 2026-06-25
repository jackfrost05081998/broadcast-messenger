import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_optional_user
from app.facebook import FacebookAPIError, FacebookConfigError, facebook_service
from app.meta_app import (
    MetaAppCredentials,
    apply_user_meta_app,
    clear_pending_meta_app,
    clear_remember_meta_app,
    get_pending_meta_app,
    resolve_meta_credentials,
    set_pending_meta_app,
    set_remember_meta_app,
)
from app.models import FacebookAccount, FacebookPage, User

router = APIRouter(tags=["auth"])
facebook_router = APIRouter(prefix="/auth/facebook", tags=["facebook"])
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
settings = get_settings()

_state_serializer = URLSafeTimedSerializer(settings.secret_key, salt="facebook-oauth")
OAUTH_STATE_MAX_AGE = 1800


def _page_picture_url(page: dict) -> str | None:
    picture = page.get("picture")
    if not isinstance(picture, dict):
        return None
    data = picture.get("data")
    if isinstance(data, dict) and data.get("url"):
        return str(data["url"])
    return None


def _page_category(page: dict) -> str | None:
    category = page.get("category")
    if category is None:
        return None
    if isinstance(category, str):
        return category[:255]
    return str(category)[:255]


def _create_oauth_state(creds: MetaAppCredentials) -> str:
    return _state_serializer.dumps(
        {
            "action": "login",
            "app_id": creds.app_id,
            "app_secret": creds.app_secret,
        }
    )


def _credentials_from_oauth_state(state: str) -> MetaAppCredentials | None:
    try:
        data = _state_serializer.loads(state, max_age=OAUTH_STATE_MAX_AGE)
        if data.get("action") != "login":
            return None
        creds = MetaAppCredentials(
            str(data.get("app_id", "")).strip(),
            str(data.get("app_secret", "")).strip(),
        )
        return creds if creds.configured else None
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def _set_session_cookie(response: RedirectResponse, user_id: int) -> RedirectResponse:
    token = create_access_token(user_id)
    secure = get_settings().app_url.startswith("https://")
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    return response


def _login_error_message(error: str | None, message: str | None = None) -> str | None:
    settings = get_settings()
    if error == "facebook_api":
        return message or "Facebook connection failed."
    if error == "invalid_oauth":
        return "Sign-in was interrupted. Please try again."
    if error == "connection_failed":
        return "Could not connect to Facebook. Please try again."
    if error == "access_denied":
        return "Facebook access was denied. Grant Page permissions to continue."
    if error == "app_not_active":
        return (
            "This Meta app is not active for your Facebook account. "
            "Paste your own App ID and App Secret below (from developers.facebook.com), save, then sign in again."
        )
    if error == "not_configured":
        return settings.facebook_config_error or "Paste your Meta App ID and App Secret on the setup page."
    return None


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    if user:
        creds = await resolve_meta_credentials(request, user)
        if creds and creds.configured and user.facebook_account:
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/setup/app", status_code=302)

    error_code = request.query_params.get("error")
    target = "/setup/app"
    if error_code:
        message = _login_error_message(error_code, request.query_params.get("message"))
        target = f"/setup/app?error={error_code}"
        if message:
            target += f"&message={quote(message)}"
    return RedirectResponse(target, status_code=302)


@router.get("/go-live", response_class=HTMLResponse)
async def go_live_page(request: Request):
    return templates.TemplateResponse(
        request,
        "go_live.html",
        {"app_name": settings.app_name},
    )


@router.get("/register")
async def register_redirect():
    return RedirectResponse("/login", status_code=302)


@router.post("/logout")
async def logout():
    redirect = RedirectResponse("/setup/app", status_code=302)
    redirect.delete_cookie(settings.session_cookie_name)
    clear_remember_meta_app(redirect)
    clear_pending_meta_app(redirect)
    return redirect


@facebook_router.get("/connect")
async def connect_facebook(
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    creds = await resolve_meta_credentials(request, user)
    if not creds or not creds.configured:
        return RedirectResponse("/setup/app", status_code=302)

    # Require a recent save so OAuth uses credentials from the form, not a stale cookie.
    if not user and not get_pending_meta_app(request):
        return RedirectResponse("/setup/app", status_code=302)

    try:
        state = _create_oauth_state(creds)
        url = facebook_service.get_login_url(state, creds)
        response = RedirectResponse(url, status_code=302)
        if not user:
            set_pending_meta_app(response, creds.app_id, creds.app_secret)
        return response
    except FacebookConfigError:
        return RedirectResponse("/setup/app", status_code=302)


@facebook_router.get("/callback")
async def facebook_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_reason: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        if error in ("access_denied", "app_not_active"):
            return RedirectResponse(f"/setup/app?error={error}", status_code=302)
        return RedirectResponse(f"/setup/app?error={error}", status_code=302)

    if not code or not state:
        return RedirectResponse("/setup/app?error=invalid_oauth", status_code=302)

    creds = _credentials_from_oauth_state(state)
    if not creds:
        creds = await resolve_meta_credentials(request, None)
    if not creds or not creds.configured:
        return RedirectResponse("/setup/app?error=not_configured", status_code=302)

    try:
        token_data = await facebook_service.exchange_code_for_token(code, creds)
        short_token = token_data.get("access_token")
        if not short_token:
            raise FacebookAPIError("No access token received")

        long_token_data = await facebook_service.get_long_lived_token(short_token, creds)
        access_token = long_token_data.get("access_token", short_token)
        expires_in = long_token_data.get("expires_in", 5184000)

        profile = await facebook_service.get_user_profile(access_token)
        facebook_user_id = profile.get("id", "")
        if not facebook_user_id:
            raise FacebookAPIError("Could not read Facebook profile")

        pages_data = await facebook_service.get_user_pages(access_token)

        result = await db.execute(select(User).where(User.facebook_user_id == facebook_user_id))
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                facebook_user_id=facebook_user_id,
                name=profile.get("name", "Facebook User"),
            )
            db.add(user)
            await db.flush()
        else:
            user.name = profile.get("name", user.name)

        apply_user_meta_app(user, creds.app_id, creds.app_secret)

        existing_pages_result = await db.execute(
            select(FacebookPage).where(FacebookPage.user_id == user.id)
        )
        existing_pages = {p.page_id: p for p in existing_pages_result.scalars().all()}

        await db.execute(delete(FacebookAccount).where(FacebookAccount.user_id == user.id))

        fb_account = FacebookAccount(
            user_id=user.id,
            facebook_user_id=facebook_user_id,
            access_token=access_token,
            token_expires_at=datetime.utcnow() + timedelta(seconds=int(expires_in)),
            name=profile.get("name"),
        )
        db.add(fb_account)

        seen_page_ids: set[str] = set()
        for page in pages_data:
            page_id = str(page.get("id", "")).strip()
            if not page_id:
                continue
            seen_page_ids.add(page_id)
            picture = _page_picture_url(page)
            page_token = page.get("access_token") or ""
            category = _page_category(page)
            await facebook_service.subscribe_page_to_messenger(page_id, page_token)

            if page_id in existing_pages:
                fb_page = existing_pages[page_id]
                fb_page.name = page.get("name") or fb_page.name
                fb_page.access_token = page_token
                fb_page.picture_url = picture
                fb_page.category = category
            else:
                db.add(
                    FacebookPage(
                        user_id=user.id,
                        page_id=page_id,
                        name=page.get("name") or "Unknown Page",
                        access_token=page_token,
                        picture_url=picture,
                        category=category,
                    )
                )

        for page_id, fb_page in existing_pages.items():
            if page_id not in seen_page_ids:
                await db.delete(fb_page)

        webhook_ok, webhook_msg = await facebook_service.ensure_app_webhook(creds)
        if webhook_ok:
            logger.info("Meta webhook ready after connect: %s", webhook_msg)
        else:
            logger.warning("Meta webhook not registered after connect: %s", webhook_msg)

        await db.commit()

    except FacebookAPIError as e:
        logger.warning("Facebook OAuth failed: %s", e)
        return RedirectResponse(
            f"/setup/app?error=facebook_api&message={quote(e.user_hint[:200])}",
            status_code=302,
        )
    except Exception as e:
        logger.exception("Facebook callback failed")
        hint = "Please try again. Re-paste App ID and Secret if the problem continues."
        return RedirectResponse(
            f"/setup/app?error=connection_failed&message={quote(hint)}",
            status_code=302,
        )

    response = RedirectResponse("/dashboard", status_code=302)
    clear_pending_meta_app(response)
    set_remember_meta_app(response, creds.app_id, creds.app_secret)
    return _set_session_cookie(response, user.id)


@facebook_router.get("/reconnect")
async def reconnect_facebook(
    request: Request,
    user: User = Depends(get_current_user),
):
    return await connect_facebook(request, user)

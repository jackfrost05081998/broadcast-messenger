"""Routes for configuring the platform Meta Developer app (.env)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    get_settings,
    is_cloud_deployment,
    is_valid_facebook_app_id,
    is_valid_facebook_app_secret,
    reload_settings,
)
from app.database import get_db
from app.dependencies import get_optional_user
from app.env_store import mask_secret, update_env_file
from app.facebook import facebook_service
from app.meta_app import (
    MetaAppCredentials,
    resolve_meta_credentials,
    save_user_meta_app,
    set_pending_meta_app,
)
from app.models import FacebookAccount, User

router = APIRouter(tags=["setup"])
templates = Jinja2Templates(directory="app/templates")


async def _discovered_apps(
    user: User | None, db: AsyncSession, configured_id: str | None
) -> list[dict]:
    if not user:
        return []
    result = await db.execute(
        select(FacebookAccount).where(FacebookAccount.user_id == user.id)
    )
    account = result.scalar_one_or_none()
    if not account or not account.access_token:
        return []
    try:
        return await facebook_service.discover_developer_apps(
            account.facebook_user_id,
            account.access_token,
            configured_id=configured_id,
        )
    except Exception:
        return []


@router.get("/setup/app", response_class=HTMLResponse)
async def app_setup_page(
    request: Request,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    creds = await resolve_meta_credentials(request, user)

    if (
        user
        and creds
        and creds.configured
        and user.facebook_account
        and not request.query_params.get("edit")
        and not request.query_params.get("error")
        and not request.query_params.get("reason")
    ):
        return RedirectResponse("/dashboard", status_code=302)

    discovered = await _discovered_apps(user, db, creds.app_id if creds else None)
    saved = request.query_params.get("saved") == "1"
    error = request.query_params.get("error")
    reason = request.query_params.get("reason")
    error_message = request.query_params.get("message")

    return templates.TemplateResponse(
        request,
        "app_setup.html",
        {
            "app_name": settings.app_name,
            "app_url": settings.app_url,
            "redirect_uri": f"{settings.app_url}/auth/facebook/callback",
            "user": user,
            "configured": bool(creds and creds.configured),
            "current_app_id": creds.app_id if creds else "",
            "has_secret": bool(creds and creds.app_secret),
            "masked_secret": mask_secret(creds.app_secret if creds else ""),
            "discovered_apps": discovered,
            "saved": saved,
            "error": error,
            "error_message": error_message,
            "reason": reason,
            "developers_url": "https://developers.facebook.com/apps/",
            "cloud_deployment": is_cloud_deployment(),
        },
    )


@router.post("/setup/app")
async def save_app_setup(
    request: Request,
    facebook_app_id: str = Form(""),
    facebook_app_secret: str = Form(""),
    sign_in_after: str = Form("1"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    existing = await resolve_meta_credentials(request, user)
    app_id = facebook_app_id.strip()
    app_secret = facebook_app_secret.strip()

    if not is_valid_facebook_app_id(app_id):
        return RedirectResponse(
            "/setup/app?error=invalid_app_id",
            status_code=302,
        )

    if not app_secret:
        if (
            existing
            and existing.configured
            and app_id == existing.app_id
            and is_valid_facebook_app_secret(existing.app_secret)
        ):
            app_secret = existing.app_secret
        elif (
            settings.facebook_configured
            and app_id == settings.facebook_app_id
            and is_valid_facebook_app_secret(settings.facebook_app_secret)
        ):
            app_secret = settings.facebook_app_secret
        else:
            return RedirectResponse(
                "/setup/app?error=missing_secret",
                status_code=302,
            )

    if not is_valid_facebook_app_secret(app_secret):
        return RedirectResponse(
            "/setup/app?error=invalid_secret",
            status_code=302,
        )

    creds = MetaAppCredentials(app_id, app_secret)

    if user:
        await save_user_meta_app(db, user, app_id, app_secret)
        return RedirectResponse("/setup/app?saved=1&edit=1", status_code=302)

    # Local-only convenience: still write .env when not on cloud.
    if not is_cloud_deployment():
        update_env_file(
            {
                "FACEBOOK_APP_ID": app_id,
                "FACEBOOK_APP_SECRET": app_secret,
            }
        )
        reload_settings()

    if sign_in_after == "1" and creds.configured:
        response = RedirectResponse("/auth/facebook/connect", status_code=303)
        set_pending_meta_app(response, app_id, app_secret)
        return response

    response = RedirectResponse("/setup/app?saved=1", status_code=302)
    set_pending_meta_app(response, app_id, app_secret)
    return response

"""Message templates and page automation settings."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_optional_user
from app.models import FacebookPage, MessageTemplate, PageAutomation, User

router = APIRouter(tags=["automation"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


def _require_user(user: User | None) -> User | RedirectResponse:
    if not user:
        return RedirectResponse("/login", status_code=302)
    return user


async def _get_page(user_id: int, page_id: str, db: AsyncSession) -> FacebookPage | None:
    result = await db.execute(
        select(FacebookPage).where(
            FacebookPage.user_id == user_id,
            FacebookPage.page_id == page_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_or_create_automation(
    user_id: int, page_id: str, db: AsyncSession
) -> PageAutomation:
    result = await db.execute(
        select(PageAutomation)
        .options(
            selectinload(PageAutomation.follow_up_template),
            selectinload(PageAutomation.reply_template),
        )
        .where(PageAutomation.user_id == user_id, PageAutomation.page_id == page_id)
    )
    automation = result.scalar_one_or_none()
    if automation:
        return automation
    automation = PageAutomation(user_id=user_id, page_id=page_id)
    db.add(automation)
    await db.flush()
    return automation


@router.get("/pages/{page_id}/automation", response_class=HTMLResponse)
async def page_automation(
    request: Request,
    page_id: str,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    page = await _get_page(user.id, page_id, db)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    automation = await _get_or_create_automation(user.id, page_id, db)
    await db.commit()

    tpl_result = await db.execute(
        select(MessageTemplate)
        .where(MessageTemplate.user_id == user.id)
        .order_by(MessageTemplate.kind, MessageTemplate.name)
    )
    all_templates = tpl_result.scalars().all()
    follow_up_templates = [t for t in all_templates if t.kind == "follow_up"]
    reply_templates = [t for t in all_templates if t.kind == "reply"]

    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse(
        request,
        "page_automation.html",
        {
            "app_name": settings.app_name,
            "user": user,
            "page": page,
            "automation": automation,
            "follow_up_templates": follow_up_templates,
            "reply_templates": reply_templates,
            "webhook_url": f"{settings.app_url}/webhook/messenger",
            "webhook_verify_token": settings.webhook_verify_token,
            "saved": saved,
        },
    )


@router.post("/pages/{page_id}/automation")
async def save_page_automation(
    page_id: str,
    follow_up_enabled: str = Form(""),
    follow_up_days: int = Form(7),
    follow_up_template_id: str = Form(""),
    reply_enabled: str = Form(""),
    reply_template_id: str = Form(""),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    page = await _get_page(user.id, page_id, db)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    automation = await _get_or_create_automation(user.id, page_id, db)
    automation.follow_up_enabled = follow_up_enabled == "1"
    automation.follow_up_days = max(1, min(follow_up_days, 90))
    automation.follow_up_template_id = (
        int(follow_up_template_id) if follow_up_template_id.strip() else None
    )
    automation.reply_enabled = reply_enabled == "1"
    automation.reply_template_id = (
        int(reply_template_id) if reply_template_id.strip() else None
    )

    await db.commit()
    return RedirectResponse(f"/pages/{page_id}/automation?saved=1", status_code=302)


@router.post("/templates")
async def create_template(
    name: str = Form(...),
    body: str = Form(...),
    kind: str = Form("general"),
    page_id: str = Form(""),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    if kind not in ("follow_up", "reply", "general"):
        kind = "general"

    template = MessageTemplate(
        user_id=user.id,
        name=name.strip()[:128],
        body=body.strip(),
        kind=kind,
    )
    db.add(template)
    await db.commit()

    redirect = f"/pages/{page_id}/automation" if page_id else "/dashboard"
    return RedirectResponse(f"{redirect}?saved=1", status_code=302)


@router.post("/templates/{template_id}/delete")
async def delete_template(
    template_id: int,
    page_id: str = Form(""),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    result = await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.id == template_id,
            MessageTemplate.user_id == user.id,
        )
    )
    template = result.scalar_one_or_none()
    if template:
        await db.delete(template)
        await db.commit()

    redirect = f"/pages/{page_id}/automation" if page_id else "/dashboard"
    return RedirectResponse(redirect, status_code=302)

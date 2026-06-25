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
from app.facebook import facebook_service
from app.meta_app import credentials_from_user
from app.models import (
    FacebookPage,
    FollowUpScheduleStep,
    MessageTemplate,
    PageAutomation,
    User,
)

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


async def _get_page_templates(
    user_id: int, page_id: str, db: AsyncSession
) -> tuple[list[MessageTemplate], list[MessageTemplate]]:
    tpl_result = await db.execute(
        select(MessageTemplate)
        .where(
            MessageTemplate.user_id == user_id,
            MessageTemplate.page_id == page_id,
        )
        .order_by(MessageTemplate.kind, MessageTemplate.name)
    )
    all_templates = tpl_result.scalars().all()
    follow_up_templates = [t for t in all_templates if t.kind == "follow_up"]
    reply_templates = [t for t in all_templates if t.kind == "reply"]
    return follow_up_templates, reply_templates


async def _get_page_template(
    user_id: int, page_id: str, template_id: int, db: AsyncSession
) -> MessageTemplate | None:
    result = await db.execute(
        select(MessageTemplate).where(
            MessageTemplate.id == template_id,
            MessageTemplate.user_id == user_id,
            MessageTemplate.page_id == page_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_follow_up_steps(
    user_id: int, page_id: str, db: AsyncSession
) -> list[FollowUpScheduleStep]:
    result = await db.execute(
        select(FollowUpScheduleStep)
        .options(selectinload(FollowUpScheduleStep.template))
        .where(
            FollowUpScheduleStep.user_id == user_id,
            FollowUpScheduleStep.page_id == page_id,
        )
        .order_by(FollowUpScheduleStep.delay_days, FollowUpScheduleStep.sort_order)
    )
    return result.scalars().all()


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

    follow_up_templates, reply_templates = await _get_page_templates(user.id, page_id, db)
    follow_up_steps = await _get_follow_up_steps(user.id, page_id, db)

    saved = request.query_params.get("saved") == "1"
    webhook_status = None
    creds = credentials_from_user(user)
    if creds:
        webhook_status = await facebook_service.get_app_webhook_status(creds)

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
            "follow_up_steps": follow_up_steps,
            "webhook_url": f"{settings.app_url}/webhook/messenger",
            "webhook_verify_token": settings.webhook_verify_token,
            "webhook_status": webhook_status,
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
    reply_cooldown_hours: int = Form(24),
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
    automation.follow_up_template_id = None
    automation.reply_template_id = None

    if follow_up_template_id.strip():
        follow_tpl = await _get_page_template(
            user.id, page_id, int(follow_up_template_id), db
        )
        if follow_tpl:
            automation.follow_up_template_id = follow_tpl.id

    automation.reply_enabled = reply_enabled == "1"
    automation.reply_cooldown_hours = max(1, min(reply_cooldown_hours, 168))
    if reply_template_id.strip():
        reply_tpl = await _get_page_template(
            user.id, page_id, int(reply_template_id), db
        )
        if reply_tpl:
            automation.reply_template_id = reply_tpl.id

    if automation.reply_enabled:
        creds = credentials_from_user(user)
        if creds:
            await facebook_service.ensure_app_webhook(creds)
        await facebook_service.subscribe_page_to_messenger(
            page.page_id, page.access_token
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

    if not page_id.strip():
        return RedirectResponse("/dashboard?error=missing_page", status_code=302)

    page = await _get_page(user.id, page_id.strip(), db)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    template = MessageTemplate(
        user_id=user.id,
        page_id=page.page_id,
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

    query = select(MessageTemplate).where(
        MessageTemplate.id == template_id,
        MessageTemplate.user_id == user.id,
    )
    if page_id.strip():
        query = query.where(MessageTemplate.page_id == page_id.strip())
    result = await db.execute(query)
    template = result.scalar_one_or_none()
    if template:
        await db.delete(template)
        await db.commit()

    redirect = f"/pages/{page_id}/automation" if page_id else "/dashboard"
    return RedirectResponse(redirect, status_code=302)


@router.post("/pages/{page_id}/automation/follow-up-step")
async def add_follow_up_step(
    page_id: str,
    delay_days: int = Form(...),
    template_id: int = Form(...),
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

    tpl = await _get_page_template(user.id, page_id, template_id, db)
    if not tpl or tpl.kind != "follow_up":
        return RedirectResponse(
            f"/pages/{page_id}/automation?error=invalid_template", status_code=302
        )

    days = max(1, min(delay_days, 90))
    existing = await _get_follow_up_steps(user.id, page_id, db)
    if any(s.delay_days == days for s in existing):
        return RedirectResponse(
            f"/pages/{page_id}/automation?error=duplicate_step", status_code=302
        )

    db.add(
        FollowUpScheduleStep(
            user_id=user.id,
            page_id=page_id,
            delay_days=days,
            template_id=tpl.id,
            sort_order=len(existing),
        )
    )
    await db.commit()
    return RedirectResponse(f"/pages/{page_id}/automation?saved=1", status_code=302)


@router.post("/pages/{page_id}/automation/follow-up-step/{step_id}/delete")
async def delete_follow_up_step(
    page_id: str,
    step_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    result = await db.execute(
        select(FollowUpScheduleStep).where(
            FollowUpScheduleStep.id == step_id,
            FollowUpScheduleStep.user_id == user.id,
            FollowUpScheduleStep.page_id == page_id,
        )
    )
    step = result.scalar_one_or_none()
    if step:
        await db.delete(step)
        await db.commit()

    return RedirectResponse(f"/pages/{page_id}/automation?saved=1", status_code=302)

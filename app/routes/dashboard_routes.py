import asyncio
import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.broadcast_service import SendPage, run_broadcast_job, resolve_broadcast_type
from app.config import get_settings
from app.contact_utils import is_within_24h_window
from app.contacts import get_page_contacts
from app.database import get_db
from app.dependencies import get_optional_user
from app.meta_app import credentials_from_user
from app.facebook import FacebookAPIError, facebook_service, normalize_recipient_psids
from app.models import Broadcast, BroadcastRecipient, FacebookPage, MessageTemplate, PageAutomation, PageContact, User

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


def _require_user(user: User | None) -> User | RedirectResponse:
    if not user:
        return RedirectResponse("/login", status_code=302)
    return user


def _wants_async_broadcast(request: Request) -> bool:
    return request.headers.get("X-Broadcast-Async") == "1"


def _broadcast_error(request: Request, page_id: str, message: str, status: int = 400):
    if _wants_async_broadcast(request):
        return JSONResponse({"error": message}, status_code=status)
    return RedirectResponse(
        f"/pages/{page_id}?error=broadcast_failed&message={quote(message)}",
        status_code=302,
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    if not user.facebook_account:
        creds = credentials_from_user(user)
        if creds and creds.configured:
            return RedirectResponse("/auth/facebook/connect", status_code=302)
        return RedirectResponse("/setup/app", status_code=302)

    error = request.query_params.get("error")
    message = f"Connection issue: {error}" if error else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_name": settings.app_name,
            "user": user,
            "pages": user.pages,
            "facebook_name": user.facebook_account.name if user.facebook_account else user.name,
            "message": message,
        },
    )


@router.get("/pages/{page_id}", response_class=HTMLResponse)
async def page_contacts(
    request: Request,
    page_id: str,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    result = await db.execute(
        select(FacebookPage).where(
            FacebookPage.user_id == user.id, FacebookPage.page_id == page_id
        )
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    contacts = []
    error = None
    messenger_ready = False
    refreshed = False
    synced_at = None
    recent_count = 0
    force_refresh = request.query_params.get("refresh") == "1"
    active_broadcast_id = request.query_params.get("broadcast_id")

    form_error = request.query_params.get("error")
    if form_error == "empty_message":
        form_error = "Please enter a message before sending."
    elif form_error == "no_recipients":
        form_error = "Select at least one contact to message."
    elif form_error == "broadcast_failed":
        form_error = request.query_params.get("message", "Broadcast failed. Please try again.")
    elif form_error == "no_follow_up_template":
        form_error = "Choose a follow-up template or create one under Automation."

    automation_result = await db.execute(
        select(PageAutomation)
        .options(selectinload(PageAutomation.follow_up_template))
        .where(PageAutomation.user_id == user.id, PageAutomation.page_id == page_id)
    )
    automation = automation_result.scalar_one_or_none()

    tpl_result = await db.execute(
        select(MessageTemplate)
        .where(
            MessageTemplate.user_id == user.id,
            MessageTemplate.page_id == page_id,
            MessageTemplate.kind == "follow_up",
        )
        .order_by(MessageTemplate.name)
    )
    follow_up_templates = tpl_result.scalars().all()

    try:
        messenger_ready = await facebook_service.is_page_subscribed(
            page.page_id, page.access_token
        )
        if not messenger_ready:
            messenger_ready = await facebook_service.subscribe_page_to_messenger(
                page.page_id, page.access_token
            )
        contacts, refreshed, synced_at = await get_page_contacts(
            db,
            user.id,
            page.page_id,
            page.access_token,
            force_refresh=force_refresh,
        )
        for contact in contacts:
            contact["within_24h"] = is_within_24h_window(contact.get("updated_time"))
        recent_count = sum(1 for c in contacts if c.get("within_24h"))
    except FacebookAPIError as e:
        error = e.user_hint
        cached_result = await db.execute(
            select(PageContact)
            .where(PageContact.user_id == user.id, PageContact.page_id == page.page_id)
            .order_by(PageContact.updated_time.desc())
        )
        rows = cached_result.scalars().all()
        if rows:
            contacts = [
                {
                    "psid": r.psid,
                    "name": r.name,
                    "updated_time": r.updated_time,
                    "message_count": r.message_count,
                    "within_24h": is_within_24h_window(r.updated_time),
                }
                for r in rows
            ]
            synced_at = max(r.synced_at for r in rows if r.synced_at)
            recent_count = sum(1 for c in contacts if c.get("within_24h"))
        else:
            recent_count = 0
    except Exception as e:
        logger.exception("Failed to load contacts for page %s", page_id)
        error = f"Unexpected error loading contacts: {e}"
        recent_count = 0

    if not active_broadcast_id:
        in_progress = await db.execute(
            select(Broadcast.id)
            .where(
                Broadcast.user_id == user.id,
                Broadcast.page_id == page_id,
                Broadcast.status == "in_progress",
            )
            .order_by(Broadcast.created_at.desc())
            .limit(1)
        )
        active_broadcast_id = in_progress.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "page_contacts.html",
        {
            "app_name": settings.app_name,
            "user": user,
            "page": page,
            "contacts": contacts,
            "recent_count": recent_count if contacts else 0,
            "error": error,
            "form_error": form_error,
            "messenger_ready": messenger_ready,
            "refreshed": refreshed,
            "synced_at": synced_at.strftime("%Y-%m-%d %H:%M") if synced_at else None,
            "automation": automation,
            "follow_up_templates": follow_up_templates,
            "active_broadcast_id": active_broadcast_id,
        },
    )


@router.post("/pages/{page_id}/broadcast")
async def broadcast_message(
    request: Request,
    page_id: str,
    message_text: str = Form(...),
    broadcast_mode: str = Form("smart"),
    messaging_type: str = Form("MESSAGE_TAG"),
    message_tag: str = Form("HUMAN_AGENT"),
    recipient_psids: Annotated[list[str] | str, Form()] = "",
    schedule_follow_up: str = Form(""),
    follow_up_template_id: str = Form(""),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    try:
        result = await db.execute(
            select(FacebookPage).where(
                FacebookPage.user_id == user.id, FacebookPage.page_id == page_id
            )
        )
        page = result.scalar_one_or_none()
        if not page:
            raise HTTPException(status_code=404, detail="Page not found")

        psids = normalize_recipient_psids(recipient_psids)
        msg_type, tag = resolve_broadcast_type(broadcast_mode, messaging_type, message_tag)

        if not message_text.strip():
            return _broadcast_error(request, page_id, "Please enter a message before sending.")

        if not psids:
            return _broadcast_error(request, page_id, "Select at least one contact to message.")

        in_progress = await db.execute(
            select(Broadcast.id).where(
                Broadcast.user_id == user.id,
                Broadcast.page_id == page_id,
                Broadcast.status == "in_progress",
            )
        )
        if in_progress.scalar_one_or_none():
            return _broadcast_error(
                request,
                page_id,
                "A broadcast is already running. Wait for it to finish.",
            )

        name_map: dict[str, str] = {}
        names_result = await db.execute(
            select(PageContact).where(
                PageContact.user_id == user.id,
                PageContact.page_id == page.page_id,
                PageContact.psid.in_(psids),
            )
        )
        for row in names_result.scalars().all():
            name_map[row.psid] = row.name

        raw_message = message_text.strip()
        should_schedule = schedule_follow_up == "1"
        follow_up_template_body = None
        follow_up_tpl_id = None
        follow_up_days = 7

        if should_schedule:
            if not follow_up_template_id.strip():
                return _broadcast_error(
                    request, page_id, "Choose a follow-up template or create one under Automation."
                )
            tpl_result = await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.id == int(follow_up_template_id),
                    MessageTemplate.user_id == user.id,
                    MessageTemplate.page_id == page.page_id,
                )
            )
            tpl = tpl_result.scalar_one_or_none()
            if not tpl:
                return _broadcast_error(
                    request, page_id, "Choose a follow-up template or create one under Automation."
                )
            follow_up_template_body = tpl.body
            follow_up_tpl_id = tpl.id
            auto_result = await db.execute(
                select(PageAutomation).where(
                    PageAutomation.user_id == user.id,
                    PageAutomation.page_id == page.page_id,
                )
            )
            auto = auto_result.scalar_one_or_none()
            if auto:
                follow_up_days = auto.follow_up_days or 7

        broadcast = Broadcast(
            user_id=user.id,
            page_id=page.page_id,
            page_name=page.name,
            message_text=raw_message,
            messaging_type=msg_type,
            message_tag=tag if msg_type == "MESSAGE_TAG" else None,
            total_recipients=len(psids),
            success_count=0,
            failure_count=0,
            status="in_progress",
        )
        db.add(broadcast)
        await db.flush()
        broadcast_id = broadcast.id

        for psid in psids:
            db.add(
                BroadcastRecipient(
                    broadcast_id=broadcast_id,
                    recipient_psid=psid,
                    recipient_name=name_map.get(psid),
                    success=None,
                )
            )
        await db.commit()

        send_page = SendPage(page_id=page.page_id, access_token=page.access_token)
        asyncio.create_task(
            run_broadcast_job(
                broadcast_id=broadcast_id,
                page=send_page,
                psids=psids,
                name_map=name_map,
                raw_message=raw_message,
                broadcast_mode=broadcast_mode,
                messaging_type=msg_type,
                message_tag=tag or None,
                schedule_follow_up=should_schedule,
                follow_up_template_body=follow_up_template_body,
                follow_up_tpl_id=follow_up_tpl_id,
                follow_up_days=follow_up_days,
                user_id=user.id,
                page_id=page.page_id,
            )
        )

        if _wants_async_broadcast(request):
            return JSONResponse({"broadcast_id": broadcast_id, "total": len(psids)})

        return RedirectResponse(
            f"/pages/{page_id}?broadcast_id={broadcast_id}",
            status_code=302,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Broadcast start failed for page %s", page_id)
        try:
            await db.rollback()
        except Exception:
            pass
        return _broadcast_error(request, page_id, str(e)[:200], status=500)


@router.get("/broadcasts/{broadcast_id}/progress")
async def broadcast_progress(
    broadcast_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user = auth

    result = await db.execute(
        select(Broadcast).where(
            Broadcast.id == broadcast_id,
            Broadcast.user_id == user.id,
        )
    )
    broadcast = result.scalar_one_or_none()
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    recipients_result = await db.execute(
        select(BroadcastRecipient).where(
            BroadcastRecipient.broadcast_id == broadcast_id
        )
    )
    recipients = recipients_result.scalars().all()

    return JSONResponse(
        {
            "status": broadcast.status,
            "total": broadcast.total_recipients,
            "done": (broadcast.success_count or 0) + (broadcast.failure_count or 0),
            "success_count": broadcast.success_count or 0,
            "failure_count": broadcast.failure_count or 0,
            "recipients": {r.recipient_psid: r.success for r in recipients},
        }
    )


@router.get("/broadcasts/{broadcast_id}", response_class=HTMLResponse)
async def broadcast_result(
    request: Request,
    broadcast_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    auth = _require_user(user)
    if not isinstance(auth, User):
        return auth
    user = auth

    result = await db.execute(
        select(Broadcast).where(
            Broadcast.id == broadcast_id, Broadcast.user_id == user.id
        )
    )
    broadcast = result.scalar_one_or_none()
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    recipients_result = await db.execute(
        select(BroadcastRecipient).where(BroadcastRecipient.broadcast_id == broadcast_id)
    )
    recipients = recipients_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "broadcast_result.html",
        {
            "app_name": settings.app_name,
            "user": user,
            "broadcast": broadcast,
            "recipients": recipients,
        },
    )

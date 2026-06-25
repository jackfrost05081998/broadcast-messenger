import asyncio
import logging
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.contact_utils import is_within_24h_window
from app.contacts import get_page_contacts
from app.database import get_db
from app.dependencies import get_optional_user
from app.meta_app import credentials_from_user
from app.facebook import (
    FacebookAPIError,
    facebook_service,
    normalize_recipient_psids,
    should_retry_as_standard_reply,
)
from app.messages import personalize_message
from app.models import Broadcast, BroadcastRecipient, FacebookPage, MessageTemplate, PageAutomation, PageContact, User
from app.scheduler import schedule_follow_ups

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()

SEND_CONCURRENCY = 8


def _require_user(user: User | None) -> User | RedirectResponse:
    if not user:
        return RedirectResponse("/login", status_code=302)
    return user


def _resolve_broadcast_type(broadcast_mode: str, messaging_type: str, message_tag: str) -> tuple[str, str]:
    """Map UI mode to Meta messaging_type + tag."""
    if broadcast_mode in ("past_inquirers", "smart"):
        return "MESSAGE_TAG", message_tag or "HUMAN_AGENT"
    if broadcast_mode == "recent":
        return "RESPONSE", ""
    if messaging_type == "MESSAGE_TAG":
        return "MESSAGE_TAG", message_tag or "HUMAN_AGENT"
    return "RESPONSE", ""


async def _try_send(
    page: FacebookPage,
    psid: str,
    message_text: str,
    messaging_type: str,
    message_tag: str | None,
) -> tuple[bool, str | None]:
    try:
        await facebook_service.send_message(
            page.page_id,
            page.access_token,
            psid,
            message_text,
            messaging_type=messaging_type,
            tag=message_tag,
        )
        return True, None
    except FacebookAPIError as e:
        return False, e.user_hint[:500]
    except Exception as e:
        logger.exception("Send failed for psid %s", psid)
        return False, str(e)[:500]


async def _send_one(
    page: FacebookPage,
    psid: str,
    message_text: str,
    broadcast_mode: str,
    messaging_type: str,
    message_tag: str | None,
    sem: asyncio.Semaphore,
) -> tuple[bool, str | None]:
    async with sem:
        msg_type, tag = _resolve_broadcast_type(broadcast_mode, messaging_type, message_tag or "")

        if broadcast_mode == "recent":
            return await _try_send(page, psid, message_text, "RESPONSE", None)

        ok, err = await _try_send(page, psid, message_text, msg_type, tag or None)
        if ok:
            return True, None

        if broadcast_mode == "smart" and err and should_retry_as_standard_reply(err):
            ok2, err2 = await _try_send(page, psid, message_text, "RESPONSE", None)
            if ok2:
                return True, None
            return False, err2 or err

        return False, err


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
        .where(MessageTemplate.user_id == user.id, MessageTemplate.kind == "follow_up")
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
        },
    )


@router.post("/pages/{page_id}/broadcast")
async def broadcast_message(
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
        msg_type, tag = _resolve_broadcast_type(broadcast_mode, messaging_type, message_tag)

        if not message_text.strip():
            return RedirectResponse(f"/pages/{page_id}?error=empty_message", status_code=302)

        if not psids:
            return RedirectResponse(f"/pages/{page_id}?error=no_recipients", status_code=302)

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
                return RedirectResponse(
                    f"/pages/{page_id}?error=no_follow_up_template", status_code=302
                )
            tpl_result = await db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.id == int(follow_up_template_id),
                    MessageTemplate.user_id == user.id,
                )
            )
            tpl = tpl_result.scalar_one_or_none()
            if not tpl:
                return RedirectResponse(
                    f"/pages/{page_id}?error=no_follow_up_template", status_code=302
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
            status="in_progress",
        )
        db.add(broadcast)
        await db.flush()

        sem = asyncio.Semaphore(SEND_CONCURRENCY)
        tasks = [
            _send_one(
                page,
                psid,
                personalize_message(name_map.get(psid), raw_message),
                broadcast_mode,
                msg_type,
                tag or None,
                sem,
            )
            for psid in psids
        ]
        results = await asyncio.gather(*tasks)

        success_count = 0
        failure_count = 0
        successful_recipients: list[tuple[str, str | None]] = []
        for psid, (ok, err) in zip(psids, results):
            db.add(
                BroadcastRecipient(
                    broadcast_id=broadcast.id,
                    recipient_psid=psid,
                    recipient_name=name_map.get(psid),
                    success=ok,
                    error_message=err,
                )
            )
            if ok:
                success_count += 1
                successful_recipients.append((psid, name_map.get(psid)))
            else:
                failure_count += 1

        broadcast.success_count = success_count
        broadcast.failure_count = failure_count
        broadcast.status = "completed"
        broadcast.completed_at = datetime.utcnow()
        await db.commit()

        if should_schedule and follow_up_template_body and successful_recipients:
            await schedule_follow_ups(
                user_id=user.id,
                page_id=page.page_id,
                recipients=successful_recipients,
                template_body=follow_up_template_body,
                template_id=follow_up_tpl_id,
                follow_up_days=follow_up_days,
                source_broadcast_id=broadcast.id,
            )

        return RedirectResponse(f"/broadcasts/{broadcast.id}", status_code=302)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Broadcast failed for page %s", page_id)
        await db.rollback()
        msg = quote(str(e)[:200])
        return RedirectResponse(
            f"/pages/{page_id}?error=broadcast_failed&message={msg}",
            status_code=302,
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

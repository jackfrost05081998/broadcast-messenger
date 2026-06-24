"""Facebook Messenger webhook — auto-reply when inquirers message your Page."""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.facebook import FacebookAPIError, facebook_service
from app.messages import personalize_message
from app.models import FacebookPage, PageAutomation, PageContact
from app.scheduler import process_due_follow_ups

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_signature(payload: bytes, signature: str | None, app_secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@router.get("/messenger")
async def verify_webhook(request: Request):
    settings = get_settings()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.webhook_verify_token and challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/messenger")
async def receive_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("object") != "page":
        return PlainTextResponse("OK")

    # Wake Render free tier on inbound messages; also drains due follow-ups.
    try:
        await process_due_follow_ups()
    except Exception:
        logger.exception("Follow-up check during webhook failed")

    for entry in data.get("entry", []):
        page_id = str(entry.get("id", ""))
        if not page_id:
            continue

        page_result = await db.execute(
            select(FacebookPage)
            .options(selectinload(FacebookPage.user))
            .where(FacebookPage.page_id == page_id)
        )
        page = page_result.scalar_one_or_none()
        if not page:
            logger.info("Webhook for unknown page %s — connect this Page in the app", page_id)
            continue

        user = page.user
        app_secret = user.meta_app_secret if user else None
        signature = request.headers.get("X-Hub-Signature-256")
        if app_secret and not _verify_signature(body, signature, app_secret):
            logger.warning(
                "Webhook signature mismatch for page %s — re-save App Secret on sign-in",
                page_id,
            )
            continue

        auto_result = await db.execute(
            select(PageAutomation)
            .options(selectinload(PageAutomation.reply_template))
            .where(
                PageAutomation.user_id == page.user_id,
                PageAutomation.page_id == page_id,
            )
        )
        automation = auto_result.scalar_one_or_none()
        if not automation:
            logger.info("No automation row for page %s", page_id)
            continue
        if not automation.reply_enabled:
            logger.info("Auto-reply disabled for page %s", page_id)
            continue
        if not automation.reply_template:
            logger.info("Auto-reply enabled but no template for page %s", page_id)
            continue

        template_body = automation.reply_template.body
        events = entry.get("messaging", [])
        logger.info("Processing %s messaging event(s) for page %s", len(events), page_id)

        for event in events:
            if event.get("message", {}).get("is_echo"):
                continue
            if not event.get("message"):
                continue

            sender_psid = str(event.get("sender", {}).get("id", ""))
            if not sender_psid or sender_psid == page_id:
                continue

            contact_result = await db.execute(
                select(PageContact).where(
                    PageContact.user_id == page.user_id,
                    PageContact.page_id == page_id,
                    PageContact.psid == sender_psid,
                )
            )
            contact = contact_result.scalar_one_or_none()
            recipient_name = contact.name if contact else None
            text = personalize_message(recipient_name, template_body)

            try:
                await facebook_service.send_message(
                    page.page_id,
                    page.access_token,
                    sender_psid,
                    text,
                    messaging_type="RESPONSE",
                )
                logger.info("Auto-reply sent to psid %s on page %s", sender_psid, page_id)
            except FacebookAPIError as exc:
                logger.warning("Auto-reply failed for psid %s: %s", sender_psid, exc.user_hint)

    return PlainTextResponse("OK")

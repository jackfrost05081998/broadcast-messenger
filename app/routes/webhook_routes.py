"""Facebook Messenger webhook — auto-reply when inquirers message your Page."""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.facebook import FacebookAPIError, facebook_service
from app.messages import personalize_message
from app.meta_app import credentials_from_env
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


def _signature_valid(payload: bytes, signature: str | None, *secrets: str | None) -> bool:
    if not signature:
        return False
    for secret in secrets:
        if secret and _verify_signature(payload, signature, secret):
            return True
    return False


def _event_inbound_at(event: dict) -> datetime:
    ts_ms = event.get("timestamp")
    if ts_ms:
        return datetime.utcfromtimestamp(int(ts_ms) / 1000)
    return datetime.utcnow()


def _should_send_auto_reply(
    contact: PageContact | None,
    inbound_at: datetime,
    interval_hours: int,
) -> bool:
    """Send auto-reply on first contact message, then again after the configured interval."""
    if not contact or not contact.auto_reply_sent_at:
        return True
    if interval_hours <= 0:
        return True
    elapsed = inbound_at - contact.auto_reply_sent_at
    return elapsed >= timedelta(hours=interval_hours)


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

    entry_count = len(data.get("entry", []))
    logger.warning(
        "Messenger webhook POST received: entries=%s",
        entry_count,
    )

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
        env_creds = credentials_from_env()
        env_secret = env_creds.app_secret if env_creds else None
        signature = request.headers.get("X-Hub-Signature-256")
        if (app_secret or env_secret) and not _signature_valid(
            body, signature, app_secret, env_secret
        ):
            logger.warning(
                "Webhook signature mismatch for page %s (header=%s) — re-save App Secret on sign-in",
                page_id,
                "present" if signature else "missing",
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
        reply_interval_hours = automation.reply_interval_hours or 0
        subscribed = await facebook_service.subscribe_page_to_messenger(
            page.page_id, page.access_token
        )
        if not subscribed:
            logger.warning(
                "Page %s not subscribed to Messenger app — check Page token",
                page_id,
            )

        events = entry.get("messaging", [])
        logger.warning(
            "Auto-reply processing %s event(s) for page %s (reply_enabled=true)",
            len(events),
            page_id,
        )

        for event in events:
            if event.get("message", {}).get("is_echo"):
                continue
            if not event.get("message"):
                continue

            sender_psid = str(event.get("sender", {}).get("id", ""))
            if not sender_psid or sender_psid == page_id:
                continue

            inbound_at = _event_inbound_at(event)

            contact_result = await db.execute(
                select(PageContact).where(
                    PageContact.user_id == page.user_id,
                    PageContact.page_id == page_id,
                    PageContact.psid == sender_psid,
                )
            )
            contact = contact_result.scalar_one_or_none()

            if not _should_send_auto_reply(contact, inbound_at, reply_interval_hours):
                if contact:
                    if contact.auto_reply_sent_at and reply_interval_hours > 0:
                        elapsed = inbound_at - contact.auto_reply_sent_at
                        logger.info(
                            "Skipping auto-reply — interval %sh not elapsed (%.0fm since last) "
                            "for psid %s on page %s",
                            reply_interval_hours,
                            elapsed.total_seconds() / 60,
                            sender_psid,
                            page_id,
                        )
                    contact.last_inbound_at = inbound_at
                continue

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
                logger.warning("Auto-reply sent to psid %s on page %s", sender_psid, page_id)
                if contact:
                    contact.auto_reply_sent_at = inbound_at
                    contact.last_inbound_at = inbound_at
                else:
                    db.add(
                        PageContact(
                            user_id=page.user_id,
                            page_id=page_id,
                            psid=sender_psid,
                            name="Unknown",
                            auto_reply_sent_at=inbound_at,
                            last_inbound_at=inbound_at,
                        )
                    )
            except FacebookAPIError as exc:
                logger.warning("Auto-reply failed for psid %s: %s", sender_psid, exc.user_hint)
                if contact:
                    contact.last_inbound_at = inbound_at

    await db.commit()
    return PlainTextResponse("OK")

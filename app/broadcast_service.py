"""Background broadcast sending with per-recipient progress."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.database import async_session
from app.facebook import (
    FacebookAPIError,
    facebook_service,
    should_retry_as_standard_reply,
)
from app.messages import personalize_message
from app.models import Broadcast, BroadcastRecipient
from app.scheduler import schedule_follow_ups

logger = logging.getLogger(__name__)

SEND_CONCURRENCY = 8


def resolve_broadcast_type(
    broadcast_mode: str, messaging_type: str, message_tag: str
) -> tuple[str, str]:
    if broadcast_mode in ("past_inquirers", "smart"):
        return "MESSAGE_TAG", message_tag or "HUMAN_AGENT"
    if broadcast_mode == "recent":
        return "RESPONSE", ""
    if messaging_type == "MESSAGE_TAG":
        return "MESSAGE_TAG", message_tag or "HUMAN_AGENT"
    return "RESPONSE", ""


@dataclass(frozen=True)
class SendPage:
    page_id: str
    access_token: str


async def try_send(
    page: SendPage,
    psid: str,
    message_text: str,
    messaging_type: str,
    message_tag: str | None,
) -> bool:
    try:
        await facebook_service.send_message(
            page.page_id,
            page.access_token,
            psid,
            message_text,
            messaging_type=messaging_type,
            tag=message_tag,
        )
        return True
    except FacebookAPIError:
        return False
    except Exception:
        logger.exception("Send failed for psid %s", psid)
        return False


async def send_one(
    page: SendPage,
    psid: str,
    message_text: str,
    broadcast_mode: str,
    messaging_type: str,
    message_tag: str | None,
    sem: asyncio.Semaphore,
) -> bool:
    async with sem:
        msg_type, tag = resolve_broadcast_type(broadcast_mode, messaging_type, message_tag or "")

        if broadcast_mode == "recent":
            return await try_send(page, psid, message_text, "RESPONSE", None)

        ok = await try_send(page, psid, message_text, msg_type, tag or None)
        if ok:
            return True

        if broadcast_mode == "smart":
            return await try_send(page, psid, message_text, "RESPONSE", None)

        return False


async def _record_recipient_result(
    broadcast_id: int, psid: str, success: bool
) -> None:
    async with async_session() as db:
        result = await db.execute(
            select(BroadcastRecipient).where(
                BroadcastRecipient.broadcast_id == broadcast_id,
                BroadcastRecipient.recipient_psid == psid,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return
        row.success = success
        broadcast = await db.get(Broadcast, broadcast_id)
        if broadcast:
            if success:
                broadcast.success_count = (broadcast.success_count or 0) + 1
            else:
                broadcast.failure_count = (broadcast.failure_count or 0) + 1
        await db.commit()


async def run_broadcast_job(
    *,
    broadcast_id: int,
    page: SendPage,
    psids: list[str],
    name_map: dict[str, str],
    raw_message: str,
    broadcast_mode: str,
    messaging_type: str,
    message_tag: str | None,
    schedule_follow_up: bool = False,
    follow_up_template_body: str | None = None,
    follow_up_tpl_id: int | None = None,
    follow_up_days: int = 7,
    user_id: int,
    page_id: str,
) -> None:
    sem = asyncio.Semaphore(SEND_CONCURRENCY)
    successful_recipients: list[tuple[str, str | None]] = []

    async def send_and_record(psid: str) -> None:
        ok = await send_one(
            page,
            psid,
            personalize_message(name_map.get(psid), raw_message),
            broadcast_mode,
            messaging_type,
            message_tag,
            sem,
        )
        await _record_recipient_result(broadcast_id, psid, ok)
        if ok:
            successful_recipients.append((psid, name_map.get(psid)))

    try:
        await asyncio.gather(*[send_and_record(psid) for psid in psids])
    except Exception:
        logger.exception("Broadcast job failed for id=%s", broadcast_id)
    finally:
        async with async_session() as db:
            broadcast = await db.get(Broadcast, broadcast_id)
            if broadcast:
                broadcast.status = "completed"
                broadcast.completed_at = datetime.utcnow()
                await db.commit()

        if schedule_follow_up and follow_up_template_body and successful_recipients:
            try:
                await schedule_follow_ups(
                    user_id=user_id,
                    page_id=page_id,
                    recipients=successful_recipients,
                    template_body=follow_up_template_body,
                    template_id=follow_up_tpl_id,
                    follow_up_days=follow_up_days,
                    source_broadcast_id=broadcast_id,
                )
            except Exception:
                logger.exception("Follow-up scheduling failed for broadcast %s", broadcast_id)

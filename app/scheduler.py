"""Process scheduled follow-up messages."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.facebook import FacebookAPIError, facebook_service, should_retry_as_standard_reply
from app.messages import personalize_message
from app.models import FacebookPage, PageAutomation, ScheduledFollowUp

logger = logging.getLogger(__name__)

SEND_CONCURRENCY = 4


async def schedule_follow_ups(
    *,
    user_id: int,
    page_id: str,
    recipients: list[tuple[str, str | None]],
    template_body: str,
    template_id: int | None,
    follow_up_days: int,
    source_broadcast_id: int | None,
) -> int:
    """Queue one follow-up per recipient after N days."""
    return await schedule_follow_up_steps(
        user_id=user_id,
        page_id=page_id,
        recipients=recipients,
        steps=[(follow_up_days, template_id, template_body.strip())],
        source_broadcast_id=source_broadcast_id,
    )


async def schedule_follow_up_steps(
    *,
    user_id: int,
    page_id: str,
    recipients: list[tuple[str, str | None]],
    steps: list[tuple[int, int | None, str]],
    source_broadcast_id: int | None,
) -> int:
    """Queue one or more follow-ups per recipient (each step = days after broadcast)."""
    now = datetime.utcnow()
    count = 0
    async with async_session() as db:
        for psid, name in recipients:
            for delay_days, template_id, template_body in steps:
                if not template_body.strip():
                    continue
                db.add(
                    ScheduledFollowUp(
                        user_id=user_id,
                        page_id=page_id,
                        recipient_psid=psid,
                        recipient_name=name,
                        template_id=template_id,
                        message_text=template_body.strip(),
                        scheduled_at=now + timedelta(days=delay_days),
                        status="pending",
                        source_broadcast_id=source_broadcast_id,
                    )
                )
                count += 1
        await db.commit()
    return count


async def _send_follow_up(
    follow_up: ScheduledFollowUp, page: FacebookPage
) -> tuple[bool, str | None]:
    text = personalize_message(follow_up.recipient_name, follow_up.message_text)
    try:
        await facebook_service.send_message(
            page.page_id,
            page.access_token,
            follow_up.recipient_psid,
            text,
            messaging_type="MESSAGE_TAG",
            tag="HUMAN_AGENT",
        )
        return True, None
    except FacebookAPIError as first_error:
        if should_retry_as_standard_reply(first_error.user_hint):
            try:
                await facebook_service.send_message(
                    page.page_id,
                    page.access_token,
                    follow_up.recipient_psid,
                    text,
                    messaging_type="RESPONSE",
                )
                return True, None
            except FacebookAPIError as retry_error:
                return False, retry_error.user_hint[:500]
        return False, first_error.user_hint[:500]
    except Exception as exc:
        logger.exception("Follow-up send failed for psid %s", follow_up.recipient_psid)
        return False, str(exc)[:500]


async def process_due_follow_ups() -> int:
    """Send all follow-ups whose scheduled time has passed. Returns count processed."""
    now = datetime.utcnow()
    processed = 0

    async with async_session() as db:
        result = await db.execute(
            select(ScheduledFollowUp)
            .where(ScheduledFollowUp.status == "pending")
            .where(ScheduledFollowUp.scheduled_at <= now)
            .order_by(ScheduledFollowUp.scheduled_at)
            .limit(50)
        )
        due = result.scalars().all()
        if not due:
            return 0

        page_cache: dict[tuple[int, str], FacebookPage | None] = {}

        for follow_up in due:
            cache_key = (follow_up.user_id, follow_up.page_id)
            if cache_key not in page_cache:
                page_result = await db.execute(
                    select(FacebookPage).where(
                        FacebookPage.user_id == follow_up.user_id,
                        FacebookPage.page_id == follow_up.page_id,
                    )
                )
                page_cache[cache_key] = page_result.scalar_one_or_none()

            page = page_cache[cache_key]
            if not page:
                follow_up.status = "failed"
                follow_up.error_message = "Facebook Page no longer connected"
                follow_up.sent_at = now
                processed += 1
                continue

            ok, err = await _send_follow_up(follow_up, page)
            follow_up.status = "sent" if ok else "failed"
            follow_up.error_message = err
            follow_up.sent_at = now
            processed += 1

        await db.commit()

    if processed:
        logger.info("Processed %s scheduled follow-up(s)", processed)
    return processed


async def automation_loop(interval_seconds: int = 3600) -> None:
    """Background loop — runs while the app process is alive."""
    while True:
        try:
            await process_due_follow_ups()
        except Exception:
            logger.exception("Follow-up scheduler error")
        await asyncio.sleep(interval_seconds)

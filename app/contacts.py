"""Fast contact sync + cache for Facebook Page conversations."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contact_utils import extract_contacts_fast
from app.config import get_settings
from app.facebook import FacebookAPIError, facebook_service
from app.models import PageContact

CACHE_TTL = timedelta(minutes=15)


async def load_cached_contacts(
    db: AsyncSession, user_id: int, page_id: str
) -> tuple[List[Dict[str, Any]], Optional[datetime]]:
    result = await db.execute(
        select(PageContact)
        .where(PageContact.user_id == user_id, PageContact.page_id == page_id)
        .order_by(PageContact.updated_time.desc())
    )
    rows = result.scalars().all()
    if not rows:
        return [], None
    synced_at = max(r.synced_at for r in rows if r.synced_at)
    contacts = [
        {
            "psid": r.psid,
            "name": r.name,
            "updated_time": r.updated_time,
            "message_count": r.message_count,
        }
        for r in rows
    ]
    return contacts, synced_at


async def save_contacts_cache(
    db: AsyncSession,
    user_id: int,
    page_id: str,
    contacts: List[Dict[str, Any]],
) -> None:
    now = datetime.utcnow()
    await db.execute(
        delete(PageContact).where(
            PageContact.user_id == user_id, PageContact.page_id == page_id
        )
    )
    for c in contacts:
        db.add(
            PageContact(
                user_id=user_id,
                page_id=page_id,
                psid=c["psid"],
                name=c.get("name", "Unknown"),
                updated_time=c.get("updated_time"),
                message_count=c.get("message_count", 0),
                synced_at=now,
            )
        )


async def sync_page_contacts(
    db: AsyncSession,
    user_id: int,
    page_id: str,
    page_access_token: str,
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    fetch_limit = limit or get_settings().max_page_contacts
    conversations = await facebook_service.get_page_conversations(
        page_id, page_access_token, limit=fetch_limit
    )
    contacts = extract_contacts_fast(conversations, page_id)
    await save_contacts_cache(db, user_id, page_id, contacts)
    await db.commit()
    return contacts


async def get_page_contacts(
    db: AsyncSession,
    user_id: int,
    page_id: str,
    page_access_token: str,
    *,
    force_refresh: bool = False,
) -> tuple[List[Dict[str, Any]], bool, Optional[datetime]]:
    """
    Return contacts, whether a background-style refresh happened, and last sync time.
    Uses cache when fresh; otherwise syncs from Facebook (fast — participants only).
    """
    cached, synced_at = await load_cached_contacts(db, user_id, page_id)
    cache_fresh = (
        synced_at is not None
        and datetime.utcnow() - synced_at < CACHE_TTL
        and len(cached) > 0
    )

    if cache_fresh and not force_refresh:
        return cached, False, synced_at

    contacts = await sync_page_contacts(db, user_id, page_id, page_access_token)
    return contacts, True, datetime.utcnow()

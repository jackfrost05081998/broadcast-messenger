from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import decode_access_token
from app.config import get_settings
from app.database import get_db
from app.models import User

settings = get_settings()


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid session")

    result = await db.execute(
        select(User)
        .options(selectinload(User.facebook_account), selectinload(User.pages))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None

    user_id = decode_access_token(token)
    if not user_id:
        return None

    result = await db.execute(
        select(User)
        .options(selectinload(User.facebook_account), selectinload(User.pages))
        .where(User.id == user_id)
    )
    return result.scalar_one_or_none()

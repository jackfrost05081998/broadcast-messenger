from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()


def create_access_token(user_id: int, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(seconds=settings.session_max_age))
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        user_id = payload.get("sub")
        return int(user_id) if user_id else None
    except (JWTError, ValueError):
        return None

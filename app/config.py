import os
import re
from functools import lru_cache

from dotenv import load_dotenv

from app.db_url import normalize_database_url
from app.env_store import reload_env

load_dotenv()

_PLACEHOLDER_PATTERNS = (
    "your_facebook_app_id",
    "your_app_id",
    "your_facebook_app_secret",
    "your_app_secret",
    "changeme",
    "xxx",
)


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(p in lowered for p in _PLACEHOLDER_PATTERNS)


def is_valid_facebook_app_id(app_id: str) -> bool:
    """Meta App IDs are numeric strings, typically 15-16 digits."""
    cleaned = app_id.strip()
    return bool(re.fullmatch(r"\d{10,20}", cleaned))


def is_valid_facebook_app_secret(secret: str) -> bool:
    cleaned = secret.strip()
    if _is_placeholder(cleaned):
        return False
    return len(cleaned) >= 16


class Settings:
    def __init__(self):
        self.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
        self.app_name = os.getenv("APP_NAME", "Broadcast Messenger")
        self.app_url = (
            os.getenv("APP_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or "http://localhost:8000"
        ).rstrip("/")
        self.port = int(os.getenv("PORT", "8000"))
        raw_db_url = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./broadcast_messenger.db"
        )
        self.database_url = normalize_database_url(raw_db_url)
        self.facebook_app_id = os.getenv("FACEBOOK_APP_ID", "").strip()
        self.facebook_app_secret = os.getenv("FACEBOOK_APP_SECRET", "").strip()
        self.facebook_api_version = os.getenv("FACEBOOK_API_VERSION", "v21.0")
        self.session_cookie_name = "session_token"
        self.session_max_age = 60 * 60 * 24 * 7
        self.webhook_verify_token = os.getenv(
            "WEBHOOK_VERIFY_TOKEN", self.secret_key[:32]
        )
        self.max_page_contacts = int(os.getenv("MAX_PAGE_CONTACTS", "5000"))

    @property
    def facebook_configured(self) -> bool:
        return is_valid_facebook_app_id(self.facebook_app_id) and is_valid_facebook_app_secret(
            self.facebook_app_secret
        )

    @property
    def facebook_config_error(self) -> str | None:
        if not self.facebook_app_id and not self.facebook_app_secret:
            return "Facebook App ID and App Secret are missing from your .env file."
        if _is_placeholder(self.facebook_app_id) or not is_valid_facebook_app_id(self.facebook_app_id):
            return (
                "FACEBOOK_APP_ID in .env is not a real App ID. "
                "It must be a numeric ID from developers.facebook.com (e.g. 123456789012345)."
            )
        if _is_placeholder(self.facebook_app_secret) or not is_valid_facebook_app_secret(
            self.facebook_app_secret
        ):
            return (
                "FACEBOOK_APP_SECRET in .env is not set. "
                "Copy the real secret from App Settings → Basic on developers.facebook.com."
            )
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_cloud_deployment() -> bool:
    """Fly.io / Render inject secrets as env vars; .env on disk is ephemeral."""
    return bool(os.getenv("FLY_APP_NAME") or os.getenv("RENDER"))


def reload_settings() -> Settings:
    reload_env()
    get_settings.cache_clear()
    return get_settings()

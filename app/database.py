from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sqlalchemy import inspect, text

from app.config import get_settings
from app.models import Base

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("postgresql"):
    if "neon.tech" in settings.database_url or "sslmode=require" in settings.database_url:
        connect_args = {"ssl": "require"}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _ensure_user_meta_columns(connection) -> None:
    inspector = inspect(connection)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "meta_app_id" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN meta_app_id VARCHAR(32)"))
    if "meta_app_secret" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN meta_app_secret TEXT"))


def _ensure_page_contact_auto_reply_columns(connection) -> None:
    inspector = inspect(connection)
    if "page_contacts" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("page_contacts")}
    if "last_inbound_at" not in columns:
        connection.execute(text("ALTER TABLE page_contacts ADD COLUMN last_inbound_at TIMESTAMP"))
    if "auto_reply_sent_at" not in columns:
        connection.execute(text("ALTER TABLE page_contacts ADD COLUMN auto_reply_sent_at TIMESTAMP"))


def _ensure_message_template_page_id(connection) -> None:
    inspector = inspect(connection)
    if "message_templates" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("message_templates")}
    if "page_id" not in columns:
        connection.execute(text("ALTER TABLE message_templates ADD COLUMN page_id VARCHAR(64)"))
        # Legacy rows: attach to the first automation page for that user when possible.
        connection.execute(
            text(
                """
                UPDATE message_templates AS t
                SET page_id = (
                    SELECT pa.page_id
                    FROM page_automations AS pa
                    WHERE pa.user_id = t.user_id
                      AND (
                        pa.follow_up_template_id = t.id
                        OR pa.reply_template_id = t.id
                      )
                    LIMIT 1
                )
                WHERE t.page_id IS NULL
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE message_templates AS t
                SET page_id = (
                    SELECT fp.page_id
                    FROM facebook_pages AS fp
                    WHERE fp.user_id = t.user_id
                    ORDER BY fp.connected_at ASC
                    LIMIT 1
                )
                WHERE t.page_id IS NULL
                """
            )
        )
        connection.execute(
            text("DELETE FROM message_templates WHERE page_id IS NULL")
        )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_user_meta_columns)
        await conn.run_sync(_ensure_page_contact_auto_reply_columns)
        await conn.run_sync(_ensure_message_template_page_id)


async def get_db():
    async with async_session() as session:
        yield session

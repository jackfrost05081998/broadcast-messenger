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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_user_meta_columns)


async def get_db():
    async with async_session() as session:
        yield session

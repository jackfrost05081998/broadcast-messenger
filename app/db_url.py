def normalize_database_url(url: str) -> str:
    """Convert standard Postgres URLs to SQLAlchemy async format."""
    cleaned = url.strip()
    if cleaned.startswith("postgres://"):
        return cleaned.replace("postgres://", "postgresql+asyncpg://", 1)
    if cleaned.startswith("postgresql://") and "+asyncpg" not in cleaned:
        return cleaned.replace("postgresql://", "postgresql+asyncpg://", 1)
    return cleaned


def is_postgres_url(url: str) -> bool:
    normalized = normalize_database_url(url)
    return normalized.startswith("postgresql")

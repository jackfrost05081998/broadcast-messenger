from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# asyncpg rejects these when passed through the URL query string.
_ASYNCPG_UNSUPPORTED_QS = frozenset({"sslmode", "channel_binding"})


def normalize_database_url(url: str) -> str:
    """Convert standard Postgres URLs to SQLAlchemy async format for asyncpg."""
    cleaned = url.strip()
    if cleaned.startswith("postgres://"):
        cleaned = cleaned.replace("postgres://", "postgresql://", 1)
    if cleaned.startswith("postgresql://") and "+asyncpg" not in cleaned:
        cleaned = cleaned.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(cleaned)
    if not parsed.query:
        return cleaned

    params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {key: val for key, val in params.items() if key not in _ASYNCPG_UNSUPPORTED_QS}
    new_query = urlencode(filtered, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def is_postgres_url(url: str) -> bool:
    cleaned = url.strip().lower()
    return cleaned.startswith("postgres://") or cleaned.startswith("postgresql://")

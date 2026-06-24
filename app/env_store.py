"""Read and update the project .env file from the setup UI."""

from pathlib import Path

from dotenv import load_dotenv, set_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = ("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET", "FACEBOOK_API_VERSION")


def env_file_path() -> Path:
    return PROJECT_ROOT / ".env"


def ensure_env_file() -> Path:
    path = env_file_path()
    if not path.exists():
        example = PROJECT_ROOT / ".env.example"
        path.write_text(example.read_text() if example.exists() else "")
    return path


def update_env_file(values: dict[str, str]) -> None:
    path = ensure_env_file()
    for key, value in values.items():
        if key not in ENV_KEYS:
            continue
        set_key(str(path), key, value.strip(), quote_mode="never")


def mask_secret(secret: str) -> str:
    cleaned = secret.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "••••••••"
    return f"{cleaned[:4]}••••{cleaned[-4:]}"


def reload_env() -> None:
    load_dotenv(dotenv_path=env_file_path(), override=True)

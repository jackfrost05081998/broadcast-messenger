"""Personalized message formatting."""


def first_name(full_name: str | None) -> str:
    if not full_name or full_name.strip().lower() == "unknown":
        return "there"
    return full_name.strip().split()[0]


def personalize_message(recipient_name: str | None, body: str) -> str:
    """Prepend a friendly greeting before the template body."""
    greeting = first_name(recipient_name)
    text = body.strip()
    if not text:
        return f"Hi {greeting},"
    return f"Hi {greeting},\n\n{text}"

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def is_within_24h_window(updated_time: Optional[str]) -> bool:
    """Heuristic: conversation updated recently may still be inside Meta's 24h reply window."""
    if not updated_time:
        return False
    try:
        normalized = updated_time.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(hours=24)
    except (TypeError, ValueError):
        return False


def extract_contacts_fast(
    conversations: List[Dict[str, Any]], page_id: str
) -> List[Dict[str, Any]]:
    contacts: List[Dict[str, Any]] = []
    seen_psids: set[str] = set()

    for conv in conversations:
        for participant in conv.get("participants", {}).get("data", []):
            psid = participant.get("id")
            if not psid or str(psid) == str(page_id) or str(psid) in seen_psids:
                continue
            seen_psids.add(str(psid))
            contacts.append(
                {
                    "psid": str(psid),
                    "name": participant.get("name", "Unknown"),
                    "updated_time": conv.get("updated_time"),
                    "message_count": conv.get("message_count", 0),
                }
            )

    contacts.sort(key=lambda c: c.get("updated_time") or "", reverse=True)
    return contacts

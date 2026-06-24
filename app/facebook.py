import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlencode

import httpx

from app.config import get_settings, is_valid_facebook_app_id
from app.contact_utils import extract_contacts_fast
from app.meta_app import MetaAppCredentials

logger = logging.getLogger(__name__)

FACEBOOK_SCOPES = ",".join(
    [
        "public_profile",
        "pages_show_list",
        "pages_messaging",
        "pages_read_engagement",
        "pages_manage_metadata",
        "business_management",
    ]
)

# Meta error_subcode → plain English (common Messenger send failures)
ERROR_SUBCODE_HINTS: Dict[int, str] = {
    2534022: "Outside the 24-hour window. They must message your Page first, then you reply within 24 hours.",
    2534013: "This Page is not linked to Instagram correctly. Use Messenger conversations only.",
    2018278: "Message tag does not match the message content. Use Smart mode or pick a matching tag.",
    2018065: "This person cannot receive messages from your app yet (Development mode — they must be an app Tester on developers.facebook.com).",
}

TAG_APPROVAL_PHRASES = (
    "without prior approval",
    "human_agent",
    "message tag",
)


def should_retry_as_standard_reply(error_message: str) -> bool:
    """Whether a failed tagged send might succeed with a standard 24-hour reply."""
    lower = error_message.lower()
    if "no matching user" in lower:
        return False
    if "invalid recipient" in lower:
        return False
    if "development mode" in lower or "app tester" in lower:
        return False
    if "outside of allowed window" in lower or "outside the 24-hour" in lower:
        return False
    if any(phrase in lower for phrase in TAG_APPROVAL_PHRASES):
        return True
    if "message tag" in lower:
        return True
    return False


class FacebookAPIError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details

    @property
    def user_hint(self) -> str:
        if not isinstance(self.details, dict):
            return str(self)
        subcode = self.details.get("error_subcode")
        if subcode and subcode in ERROR_SUBCODE_HINTS:
            return ERROR_SUBCODE_HINTS[subcode]
        code = self.details.get("code")
        msg = self.details.get("message", str(self))
        if code == 200:
            return (
                "Your Meta app is in Development mode. Recipients must be added as "
                "Testers under App roles → Roles, unless your app is Live."
            )
        if code == 10 and ("window" in msg.lower() or "policy" in msg.lower()):
            return ERROR_SUBCODE_HINTS[2534022]
        if code == 10:
            return "Permission denied. Reconnect Facebook and ensure pages_messaging is granted."
        if any(phrase in msg.lower() for phrase in TAG_APPROVAL_PHRASES):
            return (
                "Message Tag not approved for your Meta app yet. Use Smart mode for recent "
                "inquirers, or submit Human Agent permission in App Review — see /go-live."
            )
        if "24 hour" in msg.lower() or "outside of allowed window" in msg.lower():
            return ERROR_SUBCODE_HINTS[2534022]
        if "no matching user" in msg.lower():
            return (
                "This person cannot be reached (blocked, deleted account, or stale conversation). "
                "Try Refresh contacts and skip them."
            )
        if "cannot send messages to this id" in msg.lower():
            return (
                "Invalid recipient ID. Reconnect Facebook, reload contacts, and ensure "
                "this person has an open Messenger conversation with your Page."
            )
        return msg


class FacebookConfigError(Exception):
    pass


class FacebookService:
    @property
    def settings(self):
        return get_settings()

    @property
    def api_base(self) -> str:
        return f"https://graph.facebook.com/{self.settings.facebook_api_version}"

    @property
    def oauth_base(self) -> str:
        return f"https://www.facebook.com/{self.settings.facebook_api_version}"

    def _ensure_configured(self, credentials: MetaAppCredentials | None = None) -> MetaAppCredentials:
        creds = credentials or credentials_from_env()
        if not creds or not creds.configured:
            raise FacebookConfigError(
                self.settings.facebook_config_error or "Facebook not configured"
            )
        return creds

    def get_login_url(self, state: str, credentials: MetaAppCredentials | None = None) -> str:
        creds = self._ensure_configured(credentials)
        redirect_uri = f"{self.settings.app_url}/auth/facebook/callback"
        params = {
            "client_id": creds.app_id,
            "redirect_uri": redirect_uri,
            "scope": FACEBOOK_SCOPES,
            "response_type": "code",
            "state": state,
        }
        return f"{self.oauth_base}/dialog/oauth?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, credentials: MetaAppCredentials | None = None
    ) -> Dict[str, Any]:
        creds = self._ensure_configured(credentials)
        redirect_uri = f"{self.settings.app_url}/auth/facebook/callback"
        params = {
            "client_id": creds.app_id,
            "client_secret": creds.app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        return await self._get("/oauth/access_token", params)

    async def get_long_lived_token(
        self, short_token: str, credentials: MetaAppCredentials | None = None
    ) -> Dict[str, Any]:
        creds = self._ensure_configured(credentials)
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": creds.app_id,
            "client_secret": creds.app_secret,
            "fb_exchange_token": short_token,
        }
        return await self._get("/oauth/access_token", params)

    async def get_user_profile(self, access_token: str) -> Dict[str, Any]:
        return await self._get(
            "/me",
            {"fields": "id,name", "access_token": access_token},
        )

    async def get_user_pages(self, access_token: str) -> List[Dict[str, Any]]:
        data = await self._get(
            "/me/accounts",
            {
                "fields": "id,name,access_token,category,picture",
                "access_token": access_token,
            },
        )
        return data.get("data", [])

    async def get_page_conversations(
        self, page_id: str, page_access_token: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        conversations: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        # Try Messenger first, then without platform filter
        for platform in ("MESSENGER", None):
            params: Dict[str, Any] = {
                "fields": "id,updated_time,message_count,participants",
                "limit": min(limit, 100),
                "access_token": page_access_token,
            }
            if platform:
                params["platform"] = platform

            url = f"{self.api_base}/{page_id}/conversations"
            try:
                while url and len(conversations) < limit:
                    data = await self._get_url(
                        url, params if "graph.facebook.com" in url else None
                    )
                    for conv in data.get("data", []):
                        cid = conv.get("id")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            conversations.append(conv)
                        if len(conversations) >= limit:
                            break
                    url = data.get("paging", {}).get("next")
                    params = {}
                if conversations:
                    break
            except FacebookAPIError as exc:
                logger.warning("Conversations fetch platform=%s failed: %s", platform, exc)
                continue

        return conversations

    async def get_contacts_for_page(
        self, page_id: str, page_access_token: str, limit: int = 2000
    ) -> List[Dict[str, Any]]:
        conversations = await self.get_page_conversations(page_id, page_access_token, limit)
        return extract_contacts_fast(conversations, page_id)

    async def extract_contacts_from_conversations(
        self,
        conversations: List[Dict[str, Any]],
        page_id: str,
        page_access_token: str = "",
    ) -> List[Dict[str, Any]]:
        return extract_contacts_fast(conversations, page_id)

    async def send_message(
        self,
        page_id: str,
        page_access_token: str,
        recipient_psid: str,
        message_text: str,
        messaging_type: str = "RESPONSE",
        tag: Optional[str] = "ACCOUNT_UPDATE",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "recipient": {"id": str(recipient_psid)},
            "message": {"text": message_text},
        }
        if messaging_type == "RESPONSE":
            payload["messaging_type"] = "RESPONSE"
        else:
            payload["messaging_type"] = "MESSAGE_TAG"
            if tag:
                payload["tag"] = tag

        # /me/messages with Page access token is the recommended Send API path
        try:
            return await self._post(
                "/me/messages",
                {"access_token": page_access_token},
                payload,
            )
        except FacebookAPIError as first_error:
            # Fallback to explicit page-id endpoint
            logger.warning("Send via /me/messages failed, trying page endpoint: %s", first_error)
            return await self._post(
                f"/{page_id}/messages",
                {"access_token": page_access_token},
                payload,
            )

    async def subscribe_page_to_messenger(
        self, page_id: str, page_access_token: str
    ) -> bool:
        """
        Link the Page to this app for Messenger (required after adding Messenger product).
        Webhooks are NOT required — this is enough for outbound broadcasts via Send API.
        """
        params = {
            "access_token": page_access_token,
            "subscribed_fields": "messages,messaging_postbacks,message_deliveries",
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    f"{self.api_base}/{page_id}/subscribed_apps",
                    params=params,
                )
                data = self._handle_response(response)
                return bool(data.get("success", True))
        except FacebookAPIError as exc:
            logger.warning("Messenger subscribe failed for page %s: %s", page_id, exc)
            return False

    def app_access_token(self, credentials: MetaAppCredentials) -> str:
        return f"{credentials.app_id}|{credentials.app_secret}"

    async def ensure_app_webhook(
        self, credentials: MetaAppCredentials
    ) -> tuple[bool, str]:
        """Register this app's Messenger webhook callback with Meta."""
        settings = get_settings()
        callback_url = f"{settings.app_url}/webhook/messenger"
        verify_token = settings.webhook_verify_token
        app_token = self.app_access_token(credentials)
        try:
            await self._post_form(
                f"/{credentials.app_id}/subscriptions",
                {"access_token": app_token},
                {
                    "object": "page",
                    "callback_url": callback_url,
                    "verify_token": verify_token,
                    "fields": "messages,messaging_postbacks,message_deliveries",
                },
            )
            logger.info("Registered Meta webhook for app %s → %s", credentials.app_id, callback_url)
            return True, f"Webhook registered: {callback_url}"
        except FacebookAPIError as exc:
            logger.warning("Meta webhook registration failed for app %s: %s", credentials.app_id, exc)
            return False, exc.user_hint

    async def get_app_webhook_status(
        self, credentials: MetaAppCredentials
    ) -> dict[str, Any] | None:
        """Return Meta webhook subscription details for this app, if any."""
        try:
            data = await self._get(
                f"/{credentials.app_id}/subscriptions",
                {"access_token": self.app_access_token(credentials)},
            )
            settings = get_settings()
            expected_url = f"{settings.app_url}/webhook/messenger"
            for sub in data.get("data", []):
                if sub.get("object") != "page":
                    continue
                callback = sub.get("callback_url", "")
                fields = [
                    f.get("name") if isinstance(f, dict) else str(f)
                    for f in sub.get("fields", [])
                ]
                return {
                    "active": bool(sub.get("active", True)),
                    "callback_url": callback,
                    "callback_matches": callback.rstrip("/") == expected_url.rstrip("/"),
                    "expected_url": expected_url,
                    "fields": fields,
                    "messages_subscribed": "messages" in fields,
                }
            return {"active": False, "expected_url": expected_url, "callback_url": None}
        except FacebookAPIError as exc:
            logger.info("Could not read Meta webhook status: %s", exc)
            return None

    async def is_page_subscribed(
        self, page_id: str, page_access_token: str, app_id: str | None = None
    ) -> bool:
        try:
            data = await self._get(
                f"/{page_id}/subscribed_apps",
                {"access_token": page_access_token},
            )
            target_app_id = app_id or self.settings.facebook_app_id
            for entry in data.get("data", []):
                if str(entry.get("id")) == str(target_app_id):
                    return True
            return False
        except FacebookAPIError:
            return False

    async def discover_developer_apps(
        self,
        user_id: str,
        access_token: str,
        configured_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        apps: List[Dict[str, Any]] = []
        seen: set[str] = set()

        try:
            data = await self._get(
                f"/{user_id}/businesses",
                {
                    "fields": "owned_apps{id,name,link}",
                    "access_token": access_token,
                },
            )
            for business in data.get("data", []):
                for app in business.get("owned_apps", {}).get("data", []):
                    app_id = app.get("id")
                    if not app_id or str(app_id) in seen:
                        continue
                    seen.add(str(app_id))
                    apps.append(
                        {
                            "id": str(app_id),
                            "name": app.get("name") or f"App {app_id}",
                            "link": app.get("link"),
                            "source": "business",
                        }
                    )
        except FacebookAPIError as exc:
            logger.info("Could not list business apps for user %s: %s", user_id, exc)

        if configured_id and is_valid_facebook_app_id(configured_id) and configured_id not in seen:
            try:
                app = await self._get(
                    f"/{configured_id}",
                    {"fields": "id,name,link", "access_token": access_token},
                )
                apps.insert(
                    0,
                    {
                        "id": str(app.get("id", configured_id)),
                        "name": app.get("name") or "Current app",
                        "link": app.get("link"),
                        "source": "env",
                        "current": True,
                    },
                )
            except FacebookAPIError:
                apps.insert(
                    0,
                    {
                        "id": configured_id,
                        "name": "Current app",
                        "source": "env",
                        "current": True,
                    },
                )

        return apps

    async def user_can_access_app(self, app_id: str, access_token: str) -> bool:
        if not is_valid_facebook_app_id(app_id):
            return False
        try:
            await self._get(
                f"/{app_id}",
                {"fields": "id", "access_token": access_token},
            )
            return True
        except FacebookAPIError:
            return False

    async def _get_url(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.get(url, params=params)
                return self._handle_response(response)
        except FacebookAPIError:
            raise
        except httpx.HTTPError as exc:
            raise FacebookAPIError(f"Could not reach Facebook: {exc}") from exc

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        return await self._get_url(url, params)

    async def _post(
        self, path: str, params: Dict[str, Any], json_body: Dict[str, Any]
    ) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(url, params=params, json=json_body)
                return self._handle_response(response)
        except FacebookAPIError:
            raise
        except httpx.HTTPError as exc:
            raise FacebookAPIError(f"Could not reach Facebook: {exc}") from exc

    async def _post_form(
        self, path: str, params: Dict[str, Any], form: Dict[str, Any]
    ) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(url, params=params, data=form)
                return self._handle_response(response)
        except FacebookAPIError:
            raise
        except httpx.HTTPError as exc:
            raise FacebookAPIError(f"Could not reach Facebook: {exc}") from exc

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            raise FacebookAPIError("Invalid response from Facebook", response.status_code)

        if response.status_code >= 400 or "error" in data:
            error = data.get("error", {})
            message = error.get("message", "Facebook API error")
            code = error.get("code", "")
            detail = f"{message}" + (f" (code {code})" if code else "")
            logger.warning("Facebook API error: %s details=%s", detail, error)
            raise FacebookAPIError(detail, response.status_code, error)

        return data


facebook_service = FacebookService()


def credentials_from_env() -> MetaAppCredentials | None:
    settings = get_settings()
    if not settings.facebook_configured:
        return None
    return MetaAppCredentials(settings.facebook_app_id, settings.facebook_app_secret)


def normalize_recipient_psids(value: Union[list[str], str, None]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    psids = []
    for item in value:
        if isinstance(item, str) and item.strip():
            psids.append(item.strip())
    return psids

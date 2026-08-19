from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import nodriver as uc
import requests

from nodriver_utils import CSS, Locator, click_element, error_summary, find_element, find_elements, navigate


log = logging.getLogger("claw.tempmail")

BASE_URL = "https://tempmail.id.vn/api"
TEMPMAIL_API_TOKEN = "11759|B4K2XX0L9PUxXNEd4rl9Eq4702wMjJbMyA6dlLz79984c7a4"
DEFAULT_DOMAIN = "tempmail.id.vn"

INBOX_MESSAGE_LINKS: Locator = (CSS, "a[href*='/message/']")
REFRESH_INBOX_BUTTON: Locator = ("text", "Refresh")

XIAOMI_OTP_PATTERN = re.compile(
    r"verification\s+code\s*(?:is)?\s*:\s*([0-9]{4,8})",
    re.IGNORECASE,
)
CONTEXTUAL_OTP_PATTERN = re.compile(
    r"(?:otp|one[- ]?time(?:\s+pass(?:word|code))?|verification|verify|security)"
    r"[^\d]{0,80}([0-9]{4,8})",
    re.IGNORECASE,
)
GENERIC_OTP_PATTERN = re.compile(r"(?<!\d)([0-9]{4,8})(?!\d)")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
EMAIL_PATTERN = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
    re.IGNORECASE,
)


@dataclass
class TempMailInbox:
    mail_id: str = ""
    email: str = ""
    created_after: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    original_tab: uc.Tab | None = None

    # Kept for compatibility with older tests and the previous browser-based flow.
    tab: uc.Tab | None = None
    inbox_url: str = ""
    baseline_message_urls: set[str] = field(default_factory=set)


def api_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {TEMPMAIL_API_TOKEN}",
    }


def get_user_info() -> dict[str, Any]:
    response = requests.get(f"{BASE_URL}/user", headers=api_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def create_email(user: str | None = None, domain: str | None = None) -> dict[str, Any]:
    payload = {}
    if user:
        payload["user"] = user
    if domain:
        payload["domain"] = domain

    headers = {**api_headers(), "Content-Type": "application/json"}
    response = requests.post(
        f"{BASE_URL}/email/create",
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def create_email_and_get_id(
    user: str | None = None,
    domain: str | None = None,
) -> tuple[str, str]:
    result = create_email(user=user, domain=domain)
    data = result.get("data", {})
    mail_id = str(data.get("id") or "")
    email = str(data.get("email") or "")
    if not mail_id or not email:
        raise ValueError(f"Unexpected create-email response: {result}")
    return mail_id, email


def list_emails() -> dict[str, Any]:
    response = requests.get(f"{BASE_URL}/email", headers=api_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def list_messages(mail_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{BASE_URL}/email/{mail_id}",
        headers=api_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def read_message(message_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{BASE_URL}/message/{message_id}",
        headers=api_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def parse_api_time(timestamp: str) -> datetime:
    if timestamp.endswith("Z"):
        return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_message_text(message: dict[str, Any]) -> str:
    data = message.get("data", message)
    parts = []
    for key in ("subject", "text", "body", "html", "content"):
        value = data.get(key)
        if isinstance(value, str):
            parts.append(value)
    return html.unescape(HTML_TAG_PATTERN.sub(" ", "\n".join(parts)))


def extract_otp(text: str) -> str | None:
    for pattern in (XIAOMI_OTP_PATTERN, CONTEXTUAL_OTP_PATTERN):
        match = pattern.search(text)
        if match:
            return match.group(1)
    if "xiaomi" in text.casefold() or "mimo" in text.casefold():
        match = GENERIC_OTP_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def watch_new_message(
    mail_id: str,
    interval: int = 5,
    timeout: int = 120,
    sender_contains: str | None = None,
    after: datetime | str | None = None,
) -> dict[str, Any] | None:
    cutoff = (
        parse_api_time(after)
        if isinstance(after, str)
        else after or datetime.now(timezone.utc)
    )
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        messages = list_messages(mail_id)
        items = messages.get("data", {}).get("items", [])
        candidates = [
            message
            for message in items
            if parse_api_time(message["created_at"]) > cutoff
        ]
        if sender_contains:
            expected_sender = sender_contains.casefold()
            candidates = [
                message
                for message in candidates
                if expected_sender in str(message.get("from") or "").casefold()
                or expected_sender in str(message.get("sender_name") or "").casefold()
            ]

        if candidates:
            candidates.sort(key=lambda message: parse_api_time(message["created_at"]))
            return read_message(str(candidates[0]["id"]))

        time.sleep(interval)
    return None


async def prepare_tempmail_inbox(
    browser: uc.Browser,
    email: str,
    original_tab: uc.Tab,
    timeout: int = 5,
) -> TempMailInbox | None:
    del browser, timeout
    expected_email = email.strip().casefold()
    if not EMAIL_PATTERN.fullmatch(expected_email):
        raise ValueError(f"Cannot create TempMail for an invalid email address: {email}")

    user, domain = expected_email.split("@", 1)
    try:
        created_after = datetime.now(timezone.utc)
        mail_id, created_email = await asyncio.to_thread(
            create_email_and_get_id,
            user,
            domain or DEFAULT_DOMAIN,
        )
        if created_email.casefold() != expected_email:
            raise RuntimeError(
                f"TempMail created {created_email!r}, expected {expected_email!r}."
            )
        log.info("TempMail API inbox ready: %s", created_email)
        try:
            await original_tab.bring_to_front()
        except Exception:
            pass
        return TempMailInbox(
            mail_id=mail_id,
            email=created_email,
            created_after=created_after,
            original_tab=original_tab,
        )
    except Exception as error:
        log.warning("Could not prepare TempMail API inbox: %s", error_summary(error))
        try:
            await original_tab.bring_to_front()
        except Exception:
            pass
        return None


async def close_tempmail_inbox(inbox: TempMailInbox) -> None:
    if inbox.original_tab is not None:
        try:
            await inbox.original_tab.bring_to_front()
            log.debug("Returned to the login tab.")
        except Exception as error:
            log.warning("Could not restore the login tab: %s", error_summary(error))


async def wait_for_otp_from_tempmail(
    inbox: TempMailInbox,
    otp_timeout: int = 120,
) -> str | None:
    try:
        log.info("Waiting for a new TempMail API message for up to %ds...", otp_timeout)
        message = await asyncio.to_thread(
            watch_new_message,
            inbox.mail_id,
            5,
            otp_timeout,
            "xiaomi",
            inbox.created_after,
        )
        if not message:
            log.info("OTP was not received in time.")
            return None
        otp = extract_otp(extract_message_text(message))
        log.info("OTP extracted successfully." if otp else "OTP email did not contain a code.")
        return otp
    except Exception as error:
        log.warning("TempMail API OTP flow failed: %s", error_summary(error))
        return None
    finally:
        await close_tempmail_inbox(inbox)


async def refresh_inbox(tab: uc.Tab) -> None:
    refresh_buttons = await find_elements(tab, REFRESH_INBOX_BUTTON)
    if refresh_buttons:
        await click_element(refresh_buttons[0])


async def message_urls(tab: uc.Tab) -> list[str]:
    message_links = await find_elements(tab, INBOX_MESSAGE_LINKS)
    urls = []
    for link in message_links:
        href = link.attrs.get("href")
        if href:
            urls.append(urljoin(tab.target.url, href))
    return list(dict.fromkeys(urls))


async def read_current_email_text(tab: uc.Tab) -> str:
    body = await find_element(tab, (CSS, "body"), timeout=2)
    return body.text_all


async def wait_for_new_email_otp(
    inbox: TempMailInbox,
    timeout: int,
) -> str | None:
    if inbox.mail_id:
        message = await asyncio.to_thread(
            watch_new_message,
            inbox.mail_id,
            5,
            timeout,
            "xiaomi",
            inbox.created_after,
        )
        return extract_otp(extract_message_text(message)) if message else None

    tab = inbox.tab
    if tab is None:
        return None

    deadline = time.monotonic() + timeout
    processed_urls: set[str] = set()
    while time.monotonic() < deadline:
        current_urls = await message_urls(tab)
        new_urls = [
            url
            for url in current_urls
            if url not in inbox.baseline_message_urls and url not in processed_urls
        ]
        if not new_urls:
            await refresh_inbox(tab)
            await asyncio.sleep(2)
            continue

        for message_url in new_urls:
            processed_urls.add(message_url)
            await navigate(tab, message_url, timeout=10)
            otp = extract_otp(await read_current_email_text(tab))
            if otp:
                return otp
    return None

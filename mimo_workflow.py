from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import nodriver as uc

log = logging.getLogger("claw.workflow")

from nodriver_utils import (
    CSS,
    TEXT,
    Locator,
    click_element,
    click_when_present,
    error_summary,
    find_element,
    replace_input,
    set_reactive_value,
    wait_for_attribute,
    wait_until_loaded,
)
from tempmail_flow import (
    close_tempmail_inbox,
    prepare_tempmail_inbox,
    wait_for_otp_from_tempmail,
)


GOOGLE_DRIVE_DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id={file_id}"
WORKSPACE_WAIT_SECONDS = 120
POST_SEND_WAIT_SECONDS = 120
BUTTON_SETTLE_SECONDS = 2
INPUT_FOCUS_SETTLE_SECONDS = 0.5
ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

TRY_NOW_BUTTON: Locator = (
    TEXT,
    "Try Now",
)
ANNOUNCEMENT_CLOSE_BUTTON: Locator = (
    CSS,
    "button[data-track-id='claw_announcement_close_btn'], button[class*='close']",
)
COOKIE_ACCEPT_BUTTON: Locator = (
    CSS,
    "button[data-track-id='cookie_accept_all_btn']",
)
CREATE_NOW_BUTTON: Locator = (
    CSS,
    "button[data-track-id='claw_welcome_create_btn']",
)
SIGN_IN_NAVBAR_BUTTON: Locator = (
    CSS,
    "button[data-track-id='navbar_signin_btn'], a[data-track-id='navbar_signin_btn']",
)
TERMS_CHECKBOX: Locator = (CSS, "input.ant-checkbox-input[type='checkbox']")
ACCOUNT_INPUT: Locator = (CSS, "input.mi-input__input[aria-label='Email/Phone/Xiaomi Account']")
PASSWORD_INPUT: Locator = (CSS, "input.mi-input__input[aria-label='Password'], input[type='password']")
SIGN_IN_BUTTON: Locator = (
    CSS,
    "a.mi-button--primary[role='button'], button.mi-button--primary",
)
SEND_EMAIL_BUTTON: Locator = (
    CSS,
    "button.miui-btn.miui-btn-primary, button[class*='miui-btn-primary']",
)
OTP_INPUT: Locator = (CSS, "input[name='ticket'][placeholder='Enter code']")
OTP_SUBMIT_BUTTON: Locator = (
    CSS,
    "button[type='submit']:not([disabled])",
)
CREATE_CONFIRMATION_CHECKBOX: Locator = (
    CSS,
    "button[role='checkbox'][aria-disabled='false']",
)
CONTINUE_CREATING_BUTTON: Locator = (
    CSS,
    "button[data-track-id='claw_create_confirm_btn']",
)
PROMPT_TEXTAREA: Locator = (
    CSS,
    "textarea[placeholder='Ask me anything! Hold Shift+Enter to start a new line.']",
)
SEND_PROMPT_BUTTON: Locator = (
    CSS,
    "button[data-track-id='claw_send_btn']",
)
ENABLED_SEND_PROMPT_BUTTON: Locator = (
    CSS,
    "button[data-track-id='claw_send_btn']:not([disabled])",
)


def text_chunks(text: str, size: int = 120) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def normalize_textarea_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def set_prompt_textarea_value(textarea: uc.Element, prompt_text: str) -> str:
    encoded_prompt = json.dumps(prompt_text)
    return await textarea.apply(
        f"""
        element => {{
            const value = {encoded_prompt};
            element.focus();

            const prototype = HTMLTextAreaElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
            if (!setter) {{
                throw new Error('HTMLTextAreaElement value setter was not found.');
            }}

            const previousValue = element.value;
            setter.call(element, value);

            const tracker = element._valueTracker;
            if (tracker) {{
                tracker.setValue(previousValue);
            }}

            element.dispatchEvent(new InputEvent('input', {{
                bubbles: true,
                cancelable: true,
                data: value,
                inputType: 'insertText',
            }}));
            element.dispatchEvent(new Event('change', {{
                bubbles: true,
                cancelable: true,
            }}));

            return element.value;
        }}
        """
    )


async def _js_set_input(element: uc.Element, value: str) -> None:
    """Set an input value using the browser's native value setter so
    framework event listeners (Xiaomi login uses a custom framework) pick it up."""
    encoded = json.dumps(value)
    await element.apply(
        f"""
        element => {{
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set;
            if (nativeSetter) {{
                nativeSetter.call(element, {encoded});
            }} else {{
                element.value = {encoded};
            }}
            element.dispatchEvent(new Event('input',  {{ bubbles: true }}));
            element.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
        """
    )


async def fill_login_credentials(
    tab: uc.Tab,
    account: str,
    password: str,
    timeout: int = 5,
) -> bool:
    log.debug("Waiting for login inputs...")
    try:
        account_input = await find_element(tab, ACCOUNT_INPUT, timeout)
        await click_element(account_input)
        await account_input.clear_input()
        await _js_set_input(account_input, account)

        password_input = await find_element(tab, PASSWORD_INPUT, timeout)
        await click_element(password_input)
        await password_input.clear_input()
        await _js_set_input(password_input, password)
        log.info("Login credentials entered.")
        return True
    except Exception as error:
        log.warning("Could not enter login credentials: %s", error_summary(error))
        return False


async def ensure_terms_accepted(tab: uc.Tab, timeout: int = 10) -> bool:
    log.debug("Checking account terms checkbox...")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            checkbox = await find_element(tab, TERMS_CHECKBOX, timeout=3)
            checked = await checkbox.apply("element => element.checked === true")
            if checked:
                log.info("Account terms accepted.")
                return True
            await click_element(checkbox)
            await tab.sleep(BUTTON_SETTLE_SECONDS)
        except Exception as error:
            last_error = error
        await asyncio.sleep(0.25)

    summary = error_summary(last_error) if last_error else "Timed out"
    log.warning("Could not accept account terms: %s", summary)
    return False


async def submit_sign_in(tab: uc.Tab, timeout: int = 10) -> bool:
    log.debug("Submitting sign-in...")
    try:
        account_input = await find_element(tab, ACCOUNT_INPUT, timeout)
        password_input = await find_element(tab, PASSWORD_INPUT, timeout)
        await account_input.apply("element => element.blur()")
        await password_input.apply("element => element.blur()")

        sign_in_button = await find_element(tab, SIGN_IN_BUTTON, timeout)
        position = await sign_in_button.get_position()
        if not position or position.width <= 0 or position.height <= 0:
            raise RuntimeError("The visible Sign in button had no clickable position.")
        await sign_in_button.mouse_click()
        log.info("Sign-in submitted with a trusted mouse event.")
        await tab.sleep(BUTTON_SETTLE_SECONDS)

        await asyncio.sleep(5)
        try:
            await find_element(tab, SEND_EMAIL_BUTTON, timeout=1)
            return True
        except Exception:
            pass

        try:
            password_input = await find_element(tab, PASSWORD_INPUT, timeout=1)
        except Exception:
            return True

        await password_input.apply("element => element.focus()")
        key_options = {
            "code": "Enter",
            "key": "Enter",
            "windows_virtual_key_code": 13,
            "native_virtual_key_code": 13,
        }
        await tab.send(uc.cdp.input_.dispatch_key_event("rawKeyDown", **key_options))
        await tab.send(
            uc.cdp.input_.dispatch_key_event(
                "char",
                text="\r",
                unmodified_text="\r",
                **key_options,
            )
        )
        await tab.send(uc.cdp.input_.dispatch_key_event("keyUp", **key_options))
        log.info("Sign-in retried with a trusted Enter key event.")
        return True
    except Exception as error:
        log.warning("Could not submit sign-in form: %s", error_summary(error))
        return False


async def submit_otp(tab: uc.Tab, otp: str, timeout: int = 10) -> bool:
    log.debug("Waiting for the verification-code input...")
    try:
        ticket_input = await find_element(tab, OTP_INPUT, timeout)
        await replace_input(ticket_input, otp)
        submit_button = await find_element(tab, OTP_SUBMIT_BUTTON, timeout)
        await click_element(submit_button)
        log.info("OTP submitted.")
        await tab.sleep(BUTTON_SETTLE_SECONDS)
        return True
    except Exception as error:
        log.warning("Could not submit the OTP: %s", error_summary(error))
        return False


async def ensure_creation_confirmation(tab: uc.Tab, timeout: int = 10) -> bool:
    log.debug("Checking the creation confirmation...")
    try:
        checkbox = await find_element(tab, CREATE_CONFIRMATION_CHECKBOX, timeout)
        if checkbox.attrs.get("aria-checked") != "true":
            await click_element(checkbox)
            await tab.sleep(BUTTON_SETTLE_SECONDS)
            await wait_for_attribute(
                tab,
                CREATE_CONFIRMATION_CHECKBOX,
                "aria-checked",
                "true",
                timeout,
            )
            log.info("Creation confirmation checked.")
        else:
            log.info("Creation confirmation was already checked.")

        continue_button = await find_element(tab, CONTINUE_CREATING_BUTTON, timeout)
        await click_element(continue_button)
        log.info("Clicked 'Continue Creating'.")
        await tab.sleep(BUTTON_SETTLE_SECONDS)
        return True
    except Exception as error:
        log.warning("Could not confirm creation: %s", error_summary(error))
        return False


def google_drive_download_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.netloc not in {"drive.google.com", "www.drive.google.com"}:
        return url

    parts = [part for part in parsed.path.split("/") if part]
    file_id = ""
    if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
        file_id = parts[2]
    else:
        file_id = parse_qs(parsed.query).get("id", [""])[0]

    if not file_id:
        return url
    return GOOGLE_DRIVE_DOWNLOAD_URL.format(file_id=file_id)


def cache_busted_url(url: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("_prompt_ts", str(int(time.time() * 1000))))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def read_prompt_source(prompt_source: str | Path) -> str:
    source = str(prompt_source)
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        request = Request(
            cache_busted_url(google_drive_download_url(source)),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8-sig")

    return Path(source).expanduser().read_text(encoding="utf-8-sig")


def load_prompt(prompt_source: str | Path) -> str:
    prompt_text = read_prompt_source(prompt_source).strip()
    if not prompt_text:
        raise ValueError(f"Prompt source is empty: {prompt_source}")

    variable_names = set(ENV_PLACEHOLDER_PATTERN.findall(prompt_text))
    missing_names = sorted(name for name in variable_names if not os.environ.get(name))
    if missing_names:
        raise ValueError(
            "Missing environment variable(s) required by the prompt: "
            + ", ".join(missing_names)
        )
    return ENV_PLACEHOLDER_PATTERN.sub(
        lambda match: os.environ[match.group(1)], prompt_text
    )


async def send_prompt_after_creation(
    tab: uc.Tab,
    prompt_text: str,
    wait_seconds: int = WORKSPACE_WAIT_SECONDS,
    timeout: int = 30,
) -> bool:
    try:
        prompt_text = normalize_textarea_text(prompt_text)
        log.info("Prompt text: %d character(s).", len(prompt_text))

        # If still on the welcome/home page, click "Create Now" to open workspace
        log.debug("Checking for 'Create Now' on welcome page before prompt step...")
        create_now_clicked = await click_when_present(
            tab, CREATE_NOW_BUTTON, "Create Now (pre-prompt)", timeout=5
        )
        if create_now_clicked:
            log.info("Clicked 'Create Now' from welcome page; waiting for confirmation or workspace...")
            # Handle confirmation dialog if it appears
            confirmed = await click_when_present(
                tab, CONTINUE_CREATING_BUTTON, "Continue Creating", timeout=10
            )
            if not confirmed:
                log.debug("No confirmation dialog, continuing to prompt...")

        log.info("Waiting up to %ds for the workspace...", wait_seconds + timeout)
        deadline = time.monotonic() + wait_seconds + timeout
        last_error: Exception | None = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                remaining = max(1.0, deadline - time.monotonic())
                log.info(
                    "Prompt step %d: waiting for textarea (%.0fs left)...",
                    attempt,
                    remaining,
                )
                textarea = await find_element(tab, PROMPT_TEXTAREA, timeout=remaining)
                log.info("Prompt step %d: textarea found; focusing...", attempt)
                await click_element(textarea)
                await tab.sleep(INPUT_FOCUS_SETTLE_SECONDS)
                log.info("Prompt step %d: clearing textarea...", attempt)
                await textarea.clear_input()
                chunks = text_chunks(prompt_text)
                log.info(
                    "Prompt step %d: setting prompt through React textarea setter...",
                    attempt,
                )
                actual_value = await set_prompt_textarea_value(textarea, prompt_text)
                actual_length = len(actual_value or "")
                expected_length = len(prompt_text)
                log.info(
                    "Prompt step %d: React setter value length is %d/%d.",
                    attempt,
                    actual_length,
                    expected_length,
                )
                if actual_value != prompt_text:
                    log.warning(
                        "Prompt step %d: React setter mismatch; trying CDP insert_text (%d chunk(s)).",
                        attempt,
                        len(chunks),
                    )
                    await textarea.clear_input()
                for chunk_index, chunk in enumerate(chunks, start=1):
                    if actual_value == prompt_text:
                        break
                    await tab.send(uc.cdp.input_.insert_text(chunk))
                    actual_value = await textarea.apply("element => element.value")
                    log.info(
                        "Prompt step %d: chunk %d/%d inserted; value length %d/%d.",
                        attempt,
                        chunk_index,
                        len(chunks),
                        len(actual_value or ""),
                        len(prompt_text),
                    )
                actual_length = len(actual_value or "")
                expected_length = len(prompt_text)
                if prompt_text.startswith(actual_value or ""):
                    missing_text = prompt_text[actual_length:]
                    if missing_text:
                        log.info(
                            "Prompt step %d: inserting missing tail (%d char(s)).",
                            attempt,
                            len(missing_text),
                        )
                        await tab.send(uc.cdp.input_.insert_text(missing_text))
                        actual_value = await textarea.apply("element => element.value")
                        actual_length = len(actual_value or "")
                if actual_value != prompt_text:
                    log.warning(
                        "Prompt step %d: prompt value mismatch; got %d/%d chars.",
                        attempt,
                        actual_length,
                        expected_length,
                    )
                log.info(
                    "Prompt step %d: textarea value length is %d/%d.",
                    attempt,
                    actual_length,
                    expected_length,
                )
                if actual_value != prompt_text:
                    raise RuntimeError(
                        "The prompt textarea did not keep the full typed text."
                    )
                await find_element(tab, ENABLED_SEND_PROMPT_BUTTON, timeout=5)
                log.info("Prompt step %d: prompt text accepted by textarea.", attempt)
                break
            except Exception as error:
                last_error = error
                log.warning(
                    "Prompt step %d failed; retrying: %s",
                    attempt,
                    error_summary(error),
                )
                await asyncio.sleep(1)
        else:
            summary = error_summary(last_error) if last_error else "Timed out"
            raise TimeoutError(f"Prompt input did not become ready: {summary}")
        log.info("Prompt step: pressing Enter to send...")
        key_options = {
            "code": "Enter",
            "key": "Enter",
            "windows_virtual_key_code": 13,
            "native_virtual_key_code": 13,
        }
        await tab.send(uc.cdp.input_.dispatch_key_event("rawKeyDown", **key_options))
        await tab.send(
            uc.cdp.input_.dispatch_key_event(
                "char",
                text="\r",
                unmodified_text="\r",
                **key_options,
            )
        )
        await tab.send(uc.cdp.input_.dispatch_key_event("keyUp", **key_options))
        log.info("Prompt sent with Enter.")
        await tab.sleep(BUTTON_SETTLE_SECONDS)
        log.info("Waiting %ds after sending before closing the browser...", POST_SEND_WAIT_SECONDS)
        await tab.sleep(POST_SEND_WAIT_SECONDS)
        return True
    except Exception as error:
        log.warning("Could not send prompt: %s", error_summary(error))
        return False


async def prepare_verification_page(
    tab: uc.Tab,
    account: str,
    password: str,
) -> bool:
    # Dismiss any announcement popup or cookie consent if present
    await click_when_present(tab, ANNOUNCEMENT_CLOSE_BUTTON, "Announcement Close", timeout=2)
    await click_when_present(tab, COOKIE_ACCEPT_BUTTON, "Cookie Accept", timeout=1)

    # Click Create Now or Sign in button
    create_clicked = await click_when_present(tab, CREATE_NOW_BUTTON, "Create Now", timeout=15)
    if not create_clicked:
        await click_when_present(tab, SIGN_IN_NAVBAR_BUTTON, "Navbar Sign in", timeout=5)

    # Wait for Xiaomi login page to finish loading before checking checkbox
    log.debug("Waiting for Xiaomi login page to load...")
    try:
        await wait_until_loaded(tab, timeout=30, expected_url_contains="account.xiaomi.com")
    except Exception:
        pass  # Best-effort; continue even if timeout
    # Terms checkbox appears on Xiaomi login page
    if not await ensure_terms_accepted(tab, timeout=60):
        return False
    if not await fill_login_credentials(tab, account, password, timeout=15):
        return False
    if not await submit_sign_in(tab, timeout=15):
        return False
    try:
        await find_element(tab, SEND_EMAIL_BUTTON, timeout=30)
        log.info("Verification email page is ready.")
        return True
    except Exception as error:
        try:
            current_page = await tab.evaluate("window.location.href", return_by_value=True)
        except Exception:
            current_page = tab.target.url
        log.warning("Verification email page was not ready: %s", error_summary(error))
        log.info("Current page after sign-in: %s", current_page)
        return False


async def complete_creation_flow(
    tab: uc.Tab,
    otp: str,
    prompt_text: str,
    args: argparse.Namespace,
) -> bool:
    if not await submit_otp(tab, otp):
        return False

    # After OTP, two scenarios:
    # 1. New workspace: "Create Now" button appears → need to click + confirm
    # 2. Existing workspace: lands directly in chat → skip to send prompt
    log.info("Checking post-OTP state (new vs existing workspace)...")
    create_now_clicked = await click_when_present(
        tab,
        CREATE_NOW_BUTTON,
        "Create Now after OTP",
        timeout=10,
    )
    if create_now_clicked:
        log.info("New workspace flow: confirming creation...")
        confirmed = await ensure_creation_confirmation(tab)
        if not confirmed:
            log.info("Confirmation step skipped (workspace may already exist); proceeding to prompt.")
    else:
        log.info("Existing workspace detected: skipping creation confirmation.")

    return await send_prompt_after_creation(tab, prompt_text)




async def save_screenshot(tab: uc.Tab, screenshot_path: str) -> None:
    output_path = Path(screenshot_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_format = "png" if output_path.suffix.lower() == ".png" else "jpeg"
    await tab.save_screenshot(str(output_path), format=image_format)
    log.info("Screenshot saved: %s", output_path)


async def run_workflow(
    browser: uc.Browser,
    tab: uc.Tab,
    args: argparse.Namespace,
) -> bool:
    account = args.account.strip()
    password = args.password

    # Pre-load prompt BEFORE browser actions to avoid blocking the event loop later
    log.info("Pre-loading prompt from source: %s", args.prompt_source)
    try:
        loop = asyncio.get_event_loop()
        prompt_text = await loop.run_in_executor(None, load_prompt, args.prompt_source)
        log.info("Prompt pre-loaded: %d character(s).", len(prompt_text))
    except Exception as exc:
        log.error("Failed to load prompt: %s", error_summary(exc))
        return False

    log.info("Opening: %s", args.url)
    max_nav_retries = 2
    for nav_attempt in range(1, max_nav_retries + 1):
        try:
            if nav_attempt > 1:
                log.info("Navigation attempt %d/%d to %s...", nav_attempt, max_nav_retries, args.url)
                await tab.send(uc.cdp.page.navigate(args.url))
            await wait_until_loaded(tab, args.timeout)
            break
        except Exception as exc:
            # If proxy error or connection refused, fail fast so rotation switches to next proxy immediately
            is_proxy_error = any(
                k in str(exc)
                for k in ("ERR_SOCKS_", "ERR_PROXY_", "ERR_CONNECTION_REFUSED", "chrome-error://")
            )
            if is_proxy_error or nav_attempt >= max_nav_retries:
                log.error("Page load failed (fast-fail): %s", exc)
                raise exc
            log.warning("Page load attempt %d failed: %s. Retrying in 2s...", nav_attempt, exc)
            await tab.sleep(2)

    try:
        current_url = await tab.evaluate("window.location.href", return_by_value=True)
        title = await tab.evaluate("document.title", return_by_value=True)
    except Exception:
        current_url = tab.target.url
        title = tab.target.title
    log.info("Loaded URL: %s", current_url)
    log.info("Page title: %s", title)

    if not await prepare_verification_page(tab, account, password):
        return False

    inbox = await prepare_tempmail_inbox(
        browser,
        account,
        original_tab=tab,
        timeout=15,
    )
    if inbox is None:
        return False

    if not await click_when_present(tab, SEND_EMAIL_BUTTON, "Send Email"):
        await close_tempmail_inbox(inbox)
        return False

    otp = await wait_for_otp_from_tempmail(inbox, args.otp_timeout)
    completed = bool(otp) and await complete_creation_flow(tab, otp, prompt_text, args)
    if args.screenshot:
        await save_screenshot(tab, args.screenshot)
    return completed

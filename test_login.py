"""Test script: only test login flow (no workspace creation, no prompt sending)."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import nodriver as uc

from account_rotation import ProxyPool
from app_config import load_rotation_config, parse_args, parse_proxy_pool
from mimo_workflow import (
    fill_login_credentials,
    ensure_terms_accepted,
    submit_sign_in,
    prepare_verification_page,
)
from nodriver_utils import build_browser, error_summary, wait_until_loaded
from tempmail_flow import prepare_tempmail_inbox, close_tempmail_inbox

log = logging.getLogger("test_login")


async def test_login(account: str, password: str, args) -> bool:
    """Test login flow only - no workspace creation, no prompt."""
    # Disable proxy for testing
    proxy = None

    log.info("=== TEST LOGIN ===")
    log.info("Account: %s", account)
    log.info("Proxy: %s", proxy or "none")
    log.info("URL: %s", args.url)
    log.info("Headless: %s", args.headless)
    log.info("==================")

    browser = None
    try:
        # 1. Start browser
        log.info("[1/5] Starting browser...")
        browser = await asyncio.wait_for(
            build_browser(args.headless, proxy=proxy),
            timeout=30,
        )
        log.info("✓ Browser started")

        # 2. Navigate to MiMo
        log.info("[2/5] Navigating to %s...", args.url)
        tab = await asyncio.wait_for(
            browser.get(args.url),
            timeout=max(30, args.timeout),
        )
        await wait_until_loaded(tab, args.timeout)
        log.info("✓ Page loaded")

        # 3. Prepare login page (click sign-in, accept terms, fill credentials, submit)
        log.info("[3/5] Preparing verification page (sign-in + credentials)...")
        email_ready = await prepare_verification_page(tab, account, password)
        if not email_ready:
            log.error("✗ Failed to prepare verification page")
            return False
        log.info("✓ Sign-in form submitted, email verification page ready")

        # 4. Prepare temp email inbox
        log.info("[4/5] Preparing TempMail inbox...")
        inbox = await prepare_tempmail_inbox(browser, account, original_tab=tab, timeout=15)
        if inbox is None:
            log.error("✗ Failed to prepare TempMail inbox")
            return False
        log.info("✓ TempMail inbox ready: %s", inbox.email)

        # 5. Click "Send Email" button
        from mimo_workflow import click_when_present, SEND_EMAIL_BUTTON
        log.info("[5/5] Clicking 'Send Email'...")
        sent = await click_when_present(tab, SEND_EMAIL_BUTTON, "Send Email")
        if not sent:
            log.error("✗ Failed to click 'Send Email'")
            await close_tempmail_inbox(inbox)
            return False
        log.info("✓ 'Send Email' clicked")

        # Wait for OTP
        log.info("Waiting for OTP (max 120s)...")
        from tempmail_flow import wait_for_otp_from_tempmail
        otp = await wait_for_otp_from_tempmail(inbox, otp_timeout=120)
        if otp:
            log.info("✓ OTP received: %s", otp)
        else:
            log.warning("✗ OTP not received within timeout")

        log.info("=== TEST COMPLETE ===")
        log.info("Login flow: ✓ (OTP received)" if otp else "Login flow: ✗ (no OTP)")
        return bool(otp)

    except Exception as error:
        log.error("Test failed: %s", error_summary(error))
        return False
    finally:
        if browser is not None:
            try:
                browser.stop()
                log.info("Browser closed")
            except Exception:
                pass


async def main():
    # Remove --once if present (not a valid arg for parse_args)
    sys.argv = [a for a in sys.argv if a != "--once"]

    args = parse_args()

    # Use headless on Linux without display
    import os
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        args.headless = True

    # Load config, use first account only
    config_path = Path(args.config).expanduser().resolve()
    config = load_rotation_config(config_path)
    account_data = config["accounts"][0]

    success = await test_login(
        account=account_data["account"],
        password=account_data["password"],
        args=args,
    )

    log.info("Result: %s", "SUCCESS" if success else "FAILED")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("test_login.log", encoding="utf-8"),
        ],
    )
    uc.loop().run_until_complete(main())

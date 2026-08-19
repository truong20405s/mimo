from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from app_config import parse_proxy_pool
from mimo_workflow import run_workflow
from nodriver_utils import build_browser, error_summary

log = logging.getLogger("claw.rotation")
FAILED_CYCLE_BACKOFF_SECONDS = 5 * 60


class ProxyPool:
    def __init__(self, proxies: list[str]) -> None:
        self.proxies = [p.strip() for p in proxies if p.strip()]
        self.current_index = 0

    @property
    def has_proxies(self) -> bool:
        return bool(self.proxies)

    def get_current_proxy(self) -> str | None:
        if not self.proxies:
            return None
        return self.proxies[self.current_index % len(self.proxies)]

    def rotate_to_next(self) -> str | None:
        if not self.proxies:
            return None
        self.current_index = (self.current_index + 1) % len(self.proxies)
        next_proxy = self.get_current_proxy()
        log.info(
            "Rotated proxy to %d/%d: %s",
            (self.current_index % len(self.proxies)) + 1,
            len(self.proxies),
            next_proxy,
        )
        return next_proxy


async def run_account_session(
    args: argparse.Namespace,
    account: str,
    password: str,
    proxy_pool: ProxyPool | None = None,
) -> bool:
    args.account = account
    args.password = password

    # If proxy pool is provided, attempt with available proxies on failure
    max_attempts = len(proxy_pool.proxies) if (proxy_pool and proxy_pool.has_proxies) else 1

    for attempt in range(1, max_attempts + 1):
        current_proxy = proxy_pool.get_current_proxy() if proxy_pool else getattr(args, "proxy_server", None)
        if proxy_pool and proxy_pool.has_proxies:
            log.info(
                "Attempt %d/%d for %s using proxy: %s",
                attempt,
                max_attempts,
                account,
                current_proxy,
            )

        browser = None
        try:
            browser = await asyncio.wait_for(
                build_browser(args.headless, proxy=current_proxy),
                timeout=max(30, args.timeout),
            )
            try:
                tab = await asyncio.wait_for(
                    browser.get(args.url),
                    timeout=max(30, args.timeout),
                )
            except Exception as get_exc:
                log.warning("browser.get initial call had issue: %s. Using main tab...", get_exc)
                tab = browser.main_tab or (await browser.get("about:blank"))
            completed = await run_workflow(browser, tab, args)
            if completed:
                return True
            log.warning("Workflow returned incomplete for %s with proxy %s", account, current_proxy)
        except Exception as error:
            log.error("Account session attempt %d failed: %s", attempt, error_summary(error))
        finally:
            if browser is not None:
                try:
                    browser.stop()
                except Exception as error:
                    log.warning("Could not close Chrome cleanly: %s", error_summary(error))

        if proxy_pool and proxy_pool.has_proxies and attempt < max_attempts:
            log.info("Switching to next proxy in pool...")
            proxy_pool.rotate_to_next()
            await asyncio.sleep(2)

    return False


async def run_rotation(
    args: argparse.Namespace,
    accounts: list[dict[str, str]],
    interval_hours: float,
) -> None:
    interval_seconds = interval_hours * 60 * 60
    account_index = 0
    consecutive_failures = 0
    loop = asyncio.get_running_loop()

    proxy_list = parse_proxy_pool(getattr(args, "proxy_server", None))
    proxy_pool = ProxyPool(proxy_list) if proxy_list else None

    if proxy_pool and proxy_pool.has_proxies:
        log.info("Loaded %d proxy server(s) into proxy pool.", len(proxy_pool.proxies))
    else:
        log.info("No proxy configured; running direct connection.")

    log.info(
        "Rotation started: %d account(s), interval %.2fh",
        len(accounts),
        interval_hours,
    )

    while True:
        account_data = accounts[account_index]
        account = account_data["account"]
        log.info(
            "Running account %d/%d: %s",
            account_index + 1,
            len(accounts),
            account,
        )
        completed = await run_account_session(
            args, account, account_data["password"], proxy_pool=proxy_pool
        )
        if completed:
            log.info("Account completed: %s", account)
        else:
            log.warning("Account failed: %s", account)

        account_index = (account_index + 1) % len(accounts)

        if not completed:
            consecutive_failures += 1
            if consecutive_failures >= len(accounts):
                log.warning(
                    "All accounts failed; waiting %ds before retry",
                    FAILED_CYCLE_BACKOFF_SECONDS,
                )
                await asyncio.sleep(FAILED_CYCLE_BACKOFF_SECONDS)
                consecutive_failures = 0
                continue
            log.info(
                "Switching immediately to %s",
                accounts[account_index]["account"],
            )
            continue

        consecutive_failures = 0
        next_run = loop.time() + interval_seconds
        wait_seconds = max(0.0, next_run - loop.time())
        next_run_at = datetime.now().astimezone() + timedelta(seconds=wait_seconds)
        log.info(
            "Next: %s at %s (in %.2fh)",
            accounts[account_index]["account"],
            next_run_at.isoformat(timespec="seconds"),
            wait_seconds / 3600,
        )
        await asyncio.sleep(wait_seconds)


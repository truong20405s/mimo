from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

import nodriver as uc
from nodriver.core.config import find_chrome_executable

log = logging.getLogger("claw.browser")


CSS = "css"
TEXT = "text"
Locator = tuple[str, str]
DEFAULT_CLICK_SETTLE_SECONDS = 2.0


def error_summary(error: Exception) -> str:
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return lines[0] if lines else type(error).__name__


def find_chromium_binary() -> str:
    configured_path = os.environ.get("CHROME_BIN", "").strip()
    if configured_path:
        return configured_path

    browser_path = next(
        (
            path
            for name in (
                "chromium",
                "chromium-browser",
                "google-chrome",
                "chrome",
            )
            if (path := shutil.which(name))
        ),
        None,
    )
    if not browser_path:
        detected_path = find_chrome_executable()
        if detected_path and Path(detected_path).is_file():
            browser_path = str(detected_path)
    if not browser_path:
        raise RuntimeError(
            "Chrome/Chromium was not found. Set CHROME_BIN to its executable path."
        )
    return browser_path


async def build_browser(
    headless: bool,
    proxy: str | None = None,
) -> uc.Browser:
    browser_path = find_chromium_binary()
    proxy_server = proxy.strip() if proxy and proxy.strip() else None
    log.info(
        "Starting nodriver with Chromium: %s (headless=%s, proxy=%s)",
        browser_path,
        headless,
        proxy_server or "none",
    )
    browser_args = [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--window-size=1440,1000",
        "--disable-notifications",
        "--disable-popup-blocking",
    ]
    if proxy_server:
        browser_args.append(f"--proxy-server={proxy_server}")

    return await uc.start(
        headless=headless,
        browser_executable_path=browser_path,
        sandbox=False,
        browser_args=browser_args,
    )



async def find_element(
    tab: uc.Tab,
    locator: Locator,
    timeout: float = 10,
) -> uc.Element:
    strategy, selector = locator
    if strategy == CSS:
        element = await asyncio.wait_for(
            tab.select(selector, timeout=timeout),
            timeout=max(2.0, timeout + 2.0),
        )
    elif strategy == TEXT:
        matches = await find_text_elements(tab, selector, timeout)
        element = matches[0] if matches else None
    else:
        raise ValueError(f"Unsupported locator strategy: {strategy}")

    if element is None:
        raise TimeoutError(f"Element was not found after {timeout:g}s: {selector}")
    return element


async def find_elements(
    tab: uc.Tab,
    locator: Locator,
    timeout: float = 0,
) -> list[uc.Element]:
    strategy, selector = locator
    if strategy == CSS:
        return await asyncio.wait_for(
            tab.select_all(selector, timeout=timeout),
            timeout=max(2.0, timeout + 2.0),
        )
    if strategy == TEXT:
        return await find_text_elements(tab, selector, timeout)
    raise ValueError(f"Unsupported locator strategy: {strategy}")


async def find_text_elements(
    tab: uc.Tab,
    text: str,
    timeout: float,
) -> list[uc.Element]:
    deadline = time.monotonic() + timeout
    expected_text = " ".join(text.casefold().split())
    while True:
        try:
            candidates = await asyncio.wait_for(
                tab.select_all("button, a, [role='button']", timeout=0),
                timeout=2,
            )
        except Exception:
            if time.monotonic() >= deadline:
                return []
            await asyncio.sleep(0.25)
            continue

        matches = []
        for element in candidates:
            try:
                actual_text = " ".join(element.text_all.casefold().split())
                if expected_text in actual_text:
                    position = await asyncio.wait_for(
                        element.get_position(),
                        timeout=1,
                    )
                    if position and position.width > 0 and position.height > 0:
                        matches.append(
                            (actual_text != expected_text, len(actual_text), element)
                        )
            except Exception:
                continue
        if matches:
            matches.sort(key=lambda item: (item[0], item[1]))
            return [item[2] for item in matches]
        if time.monotonic() >= deadline:
            return []
        await asyncio.sleep(0.25)


async def click_element(element: uc.Element) -> None:
    await element.click()


async def click_when_present(
    tab: uc.Tab,
    locator: Locator,
    element_name: str,
    timeout: float = 3,
    settle_seconds: float = DEFAULT_CLICK_SETTLE_SECONDS,
) -> bool:
    log.debug("Waiting for '%s'...", element_name)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            remaining = max(0.5, deadline - time.monotonic())
            element = await find_element(tab, locator, min(1.0, remaining))
            await click_element(element)
            log.debug("Clicked '%s'.", element_name)
            if settle_seconds > 0:
                await tab.sleep(settle_seconds)
            return True
        except Exception as error:
            last_error = error
            await asyncio.sleep(0.25)
    summary = error_summary(last_error) if last_error else "Timed out"
    log.warning("Could not click '%s': %s", element_name, summary)
    return False


async def replace_input(element: uc.Element, value: str) -> None:
    await click_element(element)
    await element.clear_input()
    await element.send_keys(value)


async def set_reactive_value(element: uc.Element, value: str) -> None:
    encoded_value = json.dumps(value)
    actual_value = await element.apply(
        f"""
        element => {{
            const value = {encoded_value};
            const prototype = Object.getPrototypeOf(element);
            const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
            if (!setter) {{
                throw new Error('Element does not expose a value setter.');
            }}
            element.focus();
            setter.call(element, value);
            element.dispatchEvent(new InputEvent('input', {{
                bubbles: true,
                data: value,
                inputType: 'insertText',
            }}));
            element.dispatchEvent(new Event('input', {{ bubbles: true }}));
            element.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return element.value;
        }}
        """
    )
    if actual_value != value:
        raise RuntimeError("The page did not accept the requested input value.")


async def wait_until_loaded(
    tab: uc.Tab,
    timeout: float,
    expected_url_contains: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_url = ""
    last_ready_state = ""
    while time.monotonic() < deadline:
        try:
            current_url = await tab.evaluate("window.location.href", return_by_value=True)
            if current_url:
                last_url = current_url
            if not current_url or current_url in ("about:blank", "chrome://newtab/"):
                await asyncio.sleep(0.25)
                continue
            if current_url.startswith("chrome-error://"):
                error_details = ""
                try:
                    error_details = await tab.evaluate(
                        "document.body ? document.body.innerText.replace(/\\s+/g, ' ').trim() : ''",
                        return_by_value=True,
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"Browser failed to connect (URL: {current_url}). "
                    f"Network error details: {error_details or 'Connection failed / IP blocked'}. "
                    "If deploying to cloud (Railway/VPS), Xiaomi may be blocking datacenter IPs. "
                    "Please set PROXY_SERVER in environment variables."
                )
            if expected_url_contains and expected_url_contains not in current_url:
                await asyncio.sleep(0.25)
                continue
            ready_state = await tab.evaluate(
                "document.readyState", return_by_value=True
            )
            if ready_state:
                last_ready_state = ready_state
            if ready_state in ("interactive", "complete"):
                await find_element(tab, (CSS, "body"), timeout=1)
                return
        except Exception as err:
            if "chrome-error://" in str(err) or "Browser failed to connect" in str(err):
                raise
            log.debug("wait_until_loaded transient error: %s", err)
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"Page did not finish loading after {timeout:g}s. "
        f"(last_url='{last_url}', ready_state='{last_ready_state}')"
    )


async def navigate(tab: uc.Tab, url: str, timeout: float = 30) -> None:
    await tab.send(uc.cdp.page.navigate(url))
    await wait_until_loaded(tab, timeout)


async def wait_for_attribute(
    tab: uc.Tab,
    locator: Locator,
    name: str,
    expected_value: str,
    timeout: float,
) -> uc.Element:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            element = await find_element(tab, locator, timeout=0.5)
            if element.attrs.get(name) == expected_value:
                return element
        except Exception:
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"Element attribute {name!r} did not become {expected_value!r}."
    )

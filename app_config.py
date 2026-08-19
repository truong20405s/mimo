from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


STUDIO_URL = "https://aistudio.xiaomimimo.com/"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("accounts.json")
DEFAULT_PROMPT_SOURCE = (
    "https://drive.google.com/file/d/"
    "1SXbCW-6bFvVvsq70xtb_rk3thTscc2cP/view?usp=drive_link"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatically access the Xiaomi MiMo AI Studio website."
    )
    parser.add_argument("--url", default=STUDIO_URL, help="Website URL to open.")
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="Maximum seconds to wait for the page to load.",
    )

    parser.add_argument(
        "--headless", action="store_true",
        help="Run Chrome without showing a browser window.",
    )
    parser.add_argument(
        "--screenshot", default="",
        help="Optional path to save a screenshot after the page loads.",
    )

    parser.add_argument(
        "--otp-timeout", type=int, default=120,
        help="Maximum seconds to wait for the first email and its OTP.",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="JSON file containing the account rotation settings.",
    )
    parser.add_argument(
        "--prompt-source",
        default=DEFAULT_PROMPT_SOURCE,
        help="Prompt text source. Use a local path or a Google Drive/file URL.",
    )

    parser.add_argument(
        "--interval-hours", type=float, default=None,
        help="Override the rotation interval from the config file.",
    )
    parser.add_argument(
        "--proxy-server", default=None,
        help="Proxy server URL or comma-separated list (e.g. socks5://host:port,http://host:port).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser.parse_args()


def normalize_rotation_config(config: Any, source: str) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError(f"Config root from {source} must be a JSON object.")

    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError(f"Config from {source} must contain an 'accounts' list.")

    normalized_accounts = []
    for position, item in enumerate(accounts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Account #{position} from {source} must be an object.")
        if item.get("enabled", True) is False:
            continue

        account = str(item.get("account", "")).strip()
        password = str(item.get("password", ""))
        if not account or not password:
            raise ValueError(
                f"Enabled account #{position} from {source} must have "
                "'account' and 'password'."
            )
        normalized_accounts.append({"account": account, "password": password})

    if not normalized_accounts:
        raise ValueError(f"Config from {source} has no enabled accounts.")

    try:
        interval_hours = float(config.get("interval_hours", 4))
    except (TypeError, ValueError) as error:
        raise ValueError(f"'interval_hours' from {source} must be a number.") from error
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        raise ValueError(
            f"'interval_hours' from {source} must be a finite number above zero."
        )

    return {"interval_hours": interval_hours, "accounts": normalized_accounts}


def load_rotation_config(config_path: Path) -> dict[str, Any]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ValueError(f"Config file was not found: {config_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Config file is not valid JSON ({config_path}): {error}"
        ) from error

    return normalize_rotation_config(config, str(config_path))


def apply_interval_override(
    config: dict[str, Any], interval_hours: float | None
) -> None:
    if interval_hours is None:
        return
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        raise ValueError("--interval-hours must be a finite number above zero.")
    config["interval_hours"] = interval_hours


def parse_proxy_pool(proxy_arg: str | None = None) -> list[str]:
    """Parse proxy list from CLI argument or environment variables."""
    raw_proxies = (
        proxy_arg
        or os.environ.get("PROXY_POOL", "").strip()
        or os.environ.get("PROXY_SERVERS", "").strip()
        or os.environ.get("PROXY_SERVER", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or ""
    )
    if not raw_proxies:
        return []

    # Support comma, newline, or semicolon delimited proxy list
    parts = [
        item.strip()
        for chunk in raw_proxies.replace(";", ",").replace("\n", ",").split(",")
        if (item := chunk.strip())
    ]
    return parts




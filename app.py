from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import nodriver as uc

from account_rotation import run_rotation
from app_config import apply_interval_override, load_rotation_config, parse_args

log = logging.getLogger("claw")


def setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("claw.log", encoding="utf-8"),
        ],
    )


async def async_main() -> None:
    args = parse_args()
    setup_logging(getattr(args, "log_level", "INFO"))
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        args.headless = True
    config_path = Path(args.config).expanduser().resolve()
    config = load_rotation_config(config_path)
    apply_interval_override(config, args.interval_hours)
    log.info(
        "Starting rotation: %d account(s), interval %.2fh",
        len(config["accounts"]),
        config["interval_hours"],
    )
    try:
        await run_rotation(args, config["accounts"], config["interval_hours"])
    except KeyboardInterrupt:
        log.info("Rotation stopped by user.")


def main() -> None:
    uc.loop().run_until_complete(async_main())


if __name__ == "__main__":
    main()

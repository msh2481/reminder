from __future__ import annotations

import os
from pathlib import Path

from loguru import logger


def configure_logger(project_root: Path) -> None:
    """
    Configure Loguru for the daemon.

    - Output: project_root / "daemon.log"
    - Rotation: 10 MB
    - Retention: 14 days
    - Compression: zip
    - Level: INFO (override via LOG_LEVEL env var)
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_path = project_root / "daemon.log"

    logger.remove()
    logger.add(
        str(log_path),
        level=log_level,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


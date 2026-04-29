"""Centralized logging for the VEX AI Pi stack.

Call `configure_logging()` once at process start (from pushback.py).
Every module then gets its own namespaced logger via
    logging.getLogger("vexai.<component>")

Output goes to stderr, which systemd captures into the journal:

    sudo journalctl -u vexai -f
    sudo journalctl -u vexai | grep v5-data
    sudo journalctl -u vexai --output=json | jq '.MESSAGE'

The level honors VEXAI_LOG_LEVEL (default INFO).
"""

from __future__ import annotations

import logging
import os
import sys

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(default_level: str = "INFO") -> None:
    level_name = os.environ.get("VEXAI_LOG_LEVEL", default_level).upper()
    level = _LEVELS.get(level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

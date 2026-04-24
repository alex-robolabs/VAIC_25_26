"""Centralized logging configuration for the VEX AI Jetson stack.

Call configure_logging() once at process start (from pushback.py).
Every module then gets its own namespaced logger via
    logging.getLogger("vexai.<component>")

Output goes to stderr, which systemd captures into the journal.
View with:
    sudo journalctl -u vexai -f
    sudo journalctl -u vexai | grep v5-data
"""

import logging
import sys


def configure_logging(level=logging.INFO):
    """Install a stderr handler with a timestamped, namespace-tagged format.

    Idempotent: safe to call more than once; replaces any existing handlers
    on the root logger so library defaults don't double-print.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

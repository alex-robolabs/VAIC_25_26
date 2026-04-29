"""pytest configuration: add PiExample/ to sys.path so tests import the
modules under test directly (V5Comm, V5Position, serial_link, etc.)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIEXAMPLE = os.path.dirname(_HERE)
if _PIEXAMPLE not in sys.path:
    sys.path.insert(0, _PIEXAMPLE)

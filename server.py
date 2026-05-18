#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_SRC = Path(__file__).resolve().parent / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from metamcp_tools.server import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


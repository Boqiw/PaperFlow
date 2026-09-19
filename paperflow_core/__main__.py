"""支持 ``python -m paperflow_core ...``（等价于 ``python paperflow.py ...``）。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

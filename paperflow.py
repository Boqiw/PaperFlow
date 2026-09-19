#!/usr/bin/env python3
"""PaperFlow 入口脚本（薄壳）。

真正的实现放在 ``paperflow_core/`` 包里。这里只做两件事：
把包目录加进 ``sys.path``，然后调用 ``paperflow_core.cli.main``。

用法::

    python paperflow.py init --vault "D:\\Obsidian\\MyVault"
    python paperflow.py week --dry-run
    python paperflow.py week
    python paperflow.py summary --period month

也可以直接 ``python -m paperflow_core <命令>``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paperflow_core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

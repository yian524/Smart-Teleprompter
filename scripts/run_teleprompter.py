"""PyInstaller 專用的頂層 entry point。

不使用 relative import，透過 absolute import 呼叫套件內的 main。
"""

from __future__ import annotations

import sys


def main() -> int:
    from teleprompter.main import main as _main
    return _main()


if __name__ == "__main__":
    sys.exit(main())

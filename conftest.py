"""Root conftest: ensures the top-level `backtest` package (a sibling of `src/`)
is importable when tests run from the project root."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

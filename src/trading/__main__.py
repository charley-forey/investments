"""`python -m trading ...` — the supervisor spawns children this way rather than
through the `trading` console script, which need not be on PATH in a container."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

"""`python -m harness.compile --manifest ...` entrypoint."""
from __future__ import annotations

import sys

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main(["compile", *sys.argv[1:]]))

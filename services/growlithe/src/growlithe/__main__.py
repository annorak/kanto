"""Command-line entry point.

``python -m growlithe`` or the installed ``growlithe`` script both
land here. Real work happens in :mod:`growlithe.service`.
"""

from __future__ import annotations

import asyncio
import sys

from growlithe.service import amain


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

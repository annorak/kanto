"""Command-line entry point.

``python -m snorlax`` or the installed ``snorlax`` script both land
here. Real work happens in :mod:`snorlax.service`.
"""

from __future__ import annotations

import asyncio
import sys

from snorlax.service import amain


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

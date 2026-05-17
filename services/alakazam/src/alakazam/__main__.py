"""Command-line entry point.

``python -m alakazam`` or the installed ``alakazam`` script both land
here. Real work happens in :mod:`alakazam.service`.
"""

from __future__ import annotations

import asyncio
import sys

from alakazam.service import amain


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

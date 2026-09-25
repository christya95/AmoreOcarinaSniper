"""File-based kill switch checked immediately before the final submission click.

It cannot retract a request that has already been sent to Amazon; it only prevents the
click from happening. `ocarina kill` creates it, `ocarina reset --confirm` removes it.
"""

from __future__ import annotations

import time
from pathlib import Path


def engage(path: Path, reason: str = "operator kill switch") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason}\n", encoding="utf-8")


def is_engaged(path: Path) -> bool:
    return path.exists()


def clear(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False

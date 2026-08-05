"""Generic runtime inbox contract used by the agent loop."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class InboxBatch:
    """Runtime items read at a turn boundary plus their success acknowledgement."""

    items: Sequence[object]
    acknowledge: Callable[[], None]


InboxReader = Callable[[], InboxBatch | None]

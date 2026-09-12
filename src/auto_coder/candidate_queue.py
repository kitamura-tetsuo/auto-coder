"""Stable priority scheduling for pending controller candidates."""

import asyncio
from typing import Optional

from .automation_config import Candidate


class CandidateQueue(asyncio.Queue[Candidate]):
    """Serve higher priorities first without comparing mutable candidate data.

    Equal priorities retain arrival order. Already running work keeps its
    ownership; only candidates still waiting in this queue are reordered.
    """

    def __init__(self) -> None:
        super().__init__()
        self._available = {kind: asyncio.Event() for kind in ("issue", "pr")}

    async def get_for_type(self, item_type: Optional[str]) -> Candidate:
        """Wait only for the selected lane, retaining shared join accounting."""
        if item_type is None:
            return await self.get()
        available = self._available[item_type]
        while True:
            for index, candidate in enumerate(self._queue):  # type: ignore[attr-defined]
                if candidate.type == item_type or (item_type == "issue" and candidate.type == "dependency"):
                    del self._queue[index]  # type: ignore[attr-defined]
                    return candidate
            available.clear()
            await available.wait()

    def _put(self, item: Candidate) -> None:
        self._available["issue" if item.type == "dependency" else item.type].set()
        for index, queued in enumerate(self._queue):  # type: ignore[attr-defined]
            if item.priority > queued.priority:
                self._queue.insert(index, item)  # type: ignore[attr-defined]
                return
        self._queue.append(item)  # type: ignore[attr-defined]

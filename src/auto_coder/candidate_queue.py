"""Stable priority scheduling for pending controller candidates."""

import asyncio

from .automation_config import Candidate


class CandidateQueue(asyncio.Queue[Candidate]):
    """Serve higher priorities first without comparing mutable candidate data.

    Equal priorities retain arrival order. Already running work keeps its
    ownership; only candidates still waiting in this queue are reordered.
    """

    def _put(self, item: Candidate) -> None:
        for index, queued in enumerate(self._queue):
            if item.priority > queued.priority:
                self._queue.insert(index, item)
                return
        self._queue.append(item)

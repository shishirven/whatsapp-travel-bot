"""Staff notification seam.

The durable record of a handoff is the lead row. This notifier is only the
side effect (a log line today, a Slack or email call later).
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("horizon.handoff")


class StaffNotifier(Protocol):
    def handoff(self, summary: dict) -> None:
        """Tell the sales desk that a conversation needs a person."""


class LogNotifier:
    """TODO: post `summary` to the agency Slack channel or email the desk."""

    def handoff(self, summary: dict) -> None:
        logger.info(
            "HANDOFF lead=%s wa_id=%s score=%s reason=%s destination=%s",
            summary.get("lead_id"),
            summary.get("wa_id"),
            summary.get("score"),
            summary.get("reason"),
            summary.get("destination"),
        )


class ListNotifier:
    """Test double that remembers handoff summaries."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def handoff(self, summary: dict) -> None:
        self.events.append(summary)

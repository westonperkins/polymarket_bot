"""Tracks API success/failure rates for dashboard display."""

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class HealthEvent:
    timestamp: float
    source: str
    success: bool


class NetworkHealth:
    """Rolling window tracker for API health."""

    def __init__(self, window_seconds: int = 300) -> None:
        self._events: deque[HealthEvent] = deque(maxlen=500)
        self._window = window_seconds

    def record(self, source: str, success: bool) -> None:
        self._events.append(HealthEvent(
            timestamp=time.time(),
            source=source,
            success=success,
        ))

    def get_stats(self) -> dict:
        """Return health stats for the last window_seconds."""
        cutoff = time.time() - self._window
        recent = [e for e in self._events if e.timestamp >= cutoff]

        if not recent:
            return {
                "total": 0,
                "successes": 0,
                "failures": 0,
                "success_rate": 100.0,
                "by_source": {},
            }

        successes = sum(1 for e in recent if e.success)
        failures = sum(1 for e in recent if not e.success)
        total = len(recent)

        # Per-source breakdown
        sources: dict[str, dict] = {}
        for e in recent:
            if e.source not in sources:
                sources[e.source] = {"ok": 0, "fail": 0}
            if e.success:
                sources[e.source]["ok"] += 1
            else:
                sources[e.source]["fail"] += 1

        by_source = {}
        for src, counts in sources.items():
            src_total = counts["ok"] + counts["fail"]
            by_source[src] = {
                "ok": counts["ok"],
                "fail": counts["fail"],
                "rate": round(counts["ok"] / src_total * 100, 1) if src_total > 0 else 0,
            }

        return {
            "total": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / total * 100, 1) if total > 0 else 100.0,
            "by_source": by_source,
        }


# Global singleton
health = NetworkHealth()

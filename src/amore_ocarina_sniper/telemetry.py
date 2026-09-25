"""Latency telemetry with monotonic spans and JSON-lines output.

Rules: monotonic clocks (``perf_counter_ns``) are used *within* this process only.
Cross-process points (Discord message timestamp, extension ``sent_at``) are wall-clock
and are reported separately as *delivery* figures, never mixed into local spans.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ocarina")


@dataclass
class Timeline:
    """Per-attempt timeline. ``mark()`` records local monotonic offsets from creation."""

    event_id: str
    created_wall_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    _t0_ns: int = field(default_factory=time.perf_counter_ns)
    marks: dict[str, float] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str) -> float:
        offset_ms = (time.perf_counter_ns() - self._t0_ns) / 1_000_000
        self.marks[name] = round(offset_ms, 2)
        return offset_ms

    def set_delivery(
        self, *, message_ts_ms: int, sent_at_ms: int, received_wall_ms: int, detected_at_offset_ms: int
    ) -> None:
        # Wall-clock deltas between processes/hosts; skew-prone, reported as-is.
        self.delivery = {
            "message_to_extension_send_ms": sent_at_ms - message_ts_ms,
            "extension_send_to_bridge_receive_ms": received_wall_ms - sent_at_ms,
            "extension_detect_to_send_ms(local_monotonic_in_extension)": detected_at_offset_ms,
        }

    def spans(self) -> dict[str, float]:
        """Consecutive differences between marks, in insertion order."""
        out: dict[str, float] = {}
        prev_name, prev_val = "start", 0.0
        for name, val in self.marks.items():
            out[f"{prev_name}->{name}"] = round(val - prev_val, 2)
            prev_name, prev_val = name, val
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "created_wall_ms": self.created_wall_ms,
            "marks_ms": self.marks,
            "spans_ms": self.spans(),
            "delivery_wall_ms": self.delivery,
        }


class Telemetry:
    def __init__(self, log_dir: Path | None, enabled: bool = True, verbose: bool = False) -> None:
        self.enabled = enabled and log_dir is not None
        self.verbose = verbose
        self._path = (log_dir / "telemetry.jsonl") if log_dir else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> None:
        record = {"ts_ms": int(time.time() * 1000), "kind": kind, **fields}
        if self.verbose:
            log.info("telemetry %s %s", kind, json.dumps(fields, default=str))
        if not self.enabled or not self._path:
            return
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # never let telemetry break the critical path
            log.warning("telemetry write failed: %s", exc)

    def emit_timeline(self, timeline: Timeline, **fields: Any) -> None:
        self.emit("timeline", **timeline.to_dict(), **fields)

"""Best-effort realtime run event fan-out."""

from __future__ import annotations

from typing import Any

from .client import RedisStreamBus


class RunEventNotifier:
    def __init__(self, bus: RedisStreamBus) -> None:
        self.bus = bus

    def publish(self, *, run_id: str, seq: int, event_type: str, payload: dict[str, Any] | None = None, committed: bool = False) -> str:
        if seq < 0:
            raise ValueError("run event sequence must be non-negative")
        if not committed:
            raise ValueError("run event must be committed in MySQL before broadcast")
        return self.bus.publish_run_event(run_id, {"run_id": run_id, "seq": seq, "event_type": event_type, "payload": payload or {}})

    def replay(self, *, run_id: str, after_seq: int, events: list[dict[str, Any]]) -> list[str]:
        """Republish durable MySQL events after the client's last sequence."""

        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        channels: list[str] = []
        for event in sorted(events, key=lambda item: int(item.get("seq", 0))):
            seq = int(event.get("seq", 0))
            if seq <= after_seq:
                continue
            channels.append(self.publish(run_id=run_id, seq=seq, event_type=str(event.get("event_type") or ""), payload=dict(event.get("payload") or {}), committed=True))
        return channels


__all__ = ["RunEventNotifier"]

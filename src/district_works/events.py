"""领域事件定义与序列化。

所有状态变更都以事件形式追加到日志，服务重启时重放即可恢复
（包括停工 SUSPENDED 与待审批 PENDING_APPROVAL 状态）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    payload: dict[str, Any]
    id: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_line(self) -> str:
        return json.dumps(
            {"id": self.id, "seq": self.seq, "ts": self.ts,
             "type": self.type, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_line(cls, line: str) -> "Event":
        raw = json.loads(line)
        return cls(
            id=raw["id"], seq=raw["seq"], ts=raw["ts"],
            type=raw["type"], payload=raw["payload"],
        )


class EventFactory:
    """按序号生成事件，序号在重放后继续递增。"""

    def __init__(self, start_seq: int = 0) -> None:
        self._seq = count(start_seq)

    def next(self, event_type: str, payload: dict[str, Any], cmd_id: str | None = None) -> Event:
        seq = next(self._seq)
        eid = f"E-{seq:06d}" if not cmd_id else f"E-{seq:06d}:{cmd_id}"
        return Event(seq=seq, type=event_type, payload=payload, id=eid)

"""追加式事件存储：JSONL 日志 + 命令回执幂等。

- 同一 command_id 的重复提交直接返回首次回执，不产生新事件；
- 每行一个 JSON 事件，追加写入并 fsync，崩溃后重放恢复全部状态，
  包括 SUSPENDED（停工）与 PENDING_APPROVAL（待审批）；
- 完整命令回执随 COMMAND_RECEIPTED 事件落盘，跨进程重启后
  重复提交仍返回逐字一致的首次回执。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .events import Event, EventFactory


class EventStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._receipts: dict[str, dict] = {}
        self._factory = EventFactory()
        self._replay()

    # ---------- 读取 ----------

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def receipt_for(self, command_id: str) -> dict | None:
        with self._lock:
            return self._receipts.get(command_id)

    # ---------- 写入 ----------

    def append(self, event_type: str, payload: dict,
               command_id: str | None = None) -> Event:
        with self._lock:
            event = self._factory.next(event_type, payload, command_id)
            self._write_line(event.to_line())
            self._events.append(event)
            return event

    def remember_command(self, command_id: str, event: Event,
                         receipt: dict) -> dict:
        """登记幂等回执（权威持久化由 COMMAND_RECEIPTED 事件承担）。"""
        with self._lock:
            self._receipts[command_id] = receipt
            return receipt

    # ---------- 重放 ----------

    def _replay(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = Event.from_line(line)
                self._events.append(event)
                # 完整回执以 COMMAND_RECEIPTED 为准；命令的首个业务事件
                # 仅在回执事件缺失（异常崩溃）时占位。
                cid = _command_id_of(event)
                if not cid:
                    continue
                if event.type == "COMMAND_RECEIPTED":
                    self._receipts[cid] = event.payload["receipt"]
                elif cid not in self._receipts:
                    self._receipts[cid] = {
                        "command_id": cid,
                        "event_id": event.id,
                        "replayed_without_receipt": True,
                        "result": event.payload,
                    }
        if self._events:
            self._factory = EventFactory(self._events[-1].seq + 1)

    def _write_line(self, line: str) -> None:
        # O_APPEND 单行写入；锁内串行化，每次 flush+fsync，崩溃最多
        # 损失最后一条未完成的写入，已落盘行不会被后续改动覆盖。
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _command_id_of(event: Event) -> str | None:
    if ":" not in event.id:
        return None
    return event.id.split(":", 1)[1]

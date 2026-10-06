"""施工任务和通行承诺的基础契约。

保留最初的位置参数约定（``WorkTask(task_id, segment_id, depends_on,
duration_days)`` 与 ``AccessCommitment(segment_id, minimum_width_m,
always_open)``），协同后端所需的扩展字段全部带默认值追加在尾部。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkTask:
    task_id: str
    segment_id: str
    depends_on: tuple[str, ...]
    duration_days: int
    name: str = ""
    crew_id: str | None = None
    # 作业面占用的沿街宽度（米）；需要全断面封路时使用 requires_closure。
    occupies_width_m: float = 0.0
    requires_closure: bool = False
    # 邻近文保控制线/保护对象，触发文保审批级别。
    heritage_sensitive: bool = False
    # ISO 日期：允许进场的最早时间 / 最晚完工时间（开放时段约束）。
    earliest_start: str | None = None
    latest_finish: str | None = None

    def __post_init__(self) -> None:
        if self.duration_days < 1 or self.task_id in self.depends_on:
            raise ValueError("任务工期或依赖无效")
        if self.occupies_width_m < 0:
            raise ValueError("占用宽度不能为负")
        if self.earliest_start and self.latest_finish and \
                self.earliest_start > self.latest_finish:
            raise ValueError("任务允许时段无效")


@dataclass(frozen=True)
class AccessCommitment:
    segment_id: str
    minimum_width_m: float
    always_open: bool
    commitment_id: str = ""

    def __post_init__(self) -> None:
        if self.minimum_width_m <= 0:
            raise ValueError("通行宽度必须大于零")

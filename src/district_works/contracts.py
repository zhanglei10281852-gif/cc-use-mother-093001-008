"""施工任务和通行承诺的基础契约。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkTask:
    task_id: str
    segment_id: str
    depends_on: tuple[str, ...]
    duration_days: int

    def __post_init__(self) -> None:
        if self.duration_days < 1 or self.task_id in self.depends_on:
            raise ValueError("任务工期或依赖无效")


@dataclass(frozen=True)
class AccessCommitment:
    segment_id: str
    minimum_width_m: float
    always_open: bool

    def __post_init__(self) -> None:
        if self.minimum_width_m <= 0:
            raise ValueError("通行宽度必须大于零")

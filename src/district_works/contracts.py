"""施工协同的基础契约对象（值对象）。

保留早期版本的 WorkTask / AccessCommitment 构造签名，
新增字段均带默认值，不破坏既有调用与测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkTask:
    task_id: str
    segment_id: str
    depends_on: tuple[str, ...]
    duration_days: int
    crew_id: str | None = None
    # 作业占用通道宽度（米），用于通行余量校核
    occupancy_width_m: float = 0.0
    # 是否需要封闭道路才能作业（必须落入封路窗口）
    requires_closure: bool = False
    # 最早可开工日（ISO 日期字符串），缺省不限制
    earliest_start: str | None = None

    def __post_init__(self) -> None:
        if self.duration_days < 1 or self.task_id in self.depends_on:
            raise ValueError("任务工期或依赖无效")
        if self.occupancy_width_m < 0:
            raise ValueError("占用宽度不能为负")


@dataclass(frozen=True)
class Segment:
    segment_id: str
    name: str
    width_m: float
    # 空间上相互干扰的区段（同一空间组同日只能有一处占用）
    zone_ids: tuple[str, ...] = ()
    # 文保控制线内
    heritage_control: bool = False


@dataclass(frozen=True)
class Crew:
    crew_id: str
    name: str
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProtectionObject:
    """保护对象：古树、老宅墙基、已知/未知管线等。"""
    object_id: str
    segment_id: str
    kind: str  # heritage / utility / unknown
    name: str = ""
    buffer_m: float = 0.0
    required_level: int = 2  # 触碰时所需审批级别


@dataclass(frozen=True)
class AccessCommitment:
    """对居民/商户的通行承诺。"""
    segment_id: str
    minimum_width_m: float
    always_open: bool

    def __post_init__(self) -> None:
        if self.minimum_width_m <= 0:
            raise ValueError("通行宽度必须大于零")


@dataclass(frozen=True)
class ClosureWindow:
    """批准的封路窗口：requires_closure 的任务必须整体落入其中。"""
    segment_id: str
    start_date: str  # ISO date
    end_date: str


@dataclass(frozen=True)
class OpenWindow:
    """开放时段约束。

    work_allowed=False 表示当日该区段必须保持开放（集市、节庆、
    居民通行承诺时段），不得安排作业；True 可用于登记特批作业日。
    """
    segment_id: str
    date: str
    work_allowed: bool = False
    note: str = ""


@dataclass(frozen=True)
class Milestone:
    milestone_id: str
    name: str
    due_date: str
    task_ids: tuple[str, ...] = field(default_factory=tuple)

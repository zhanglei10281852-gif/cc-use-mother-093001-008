"""领域模型：区段、队伍、保护对象、封路窗口、里程碑与审批/冲突枚举。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ApprovalLevel(enum.Enum):
    """变更/复工的审批级别，rank 越大权限越高。"""

    SITE = ("SITE", 1, "现场级")
    OFFICE = ("OFFICE", 2, "街区更新办公室")
    HERITAGE = ("HERITAGE", 3, "文保主管部门")

    def __new__(cls, code: str, rank: int, label: str):
        obj = object.__new__(cls)
        obj._value_ = code
        obj.rank = rank
        obj.label = label
        return obj

    @classmethod
    def of(cls, code: str) -> "ApprovalLevel":
        for level in cls:
            if level.value == code or level.name == code:
                return level
        raise ValueError(f"未知审批级别: {code}")


class TaskStatus(enum.Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class ConflictType(enum.Enum):
    RESOURCE = "RESOURCE"      # 同一施工队同一时段重复派工
    SPACE = "SPACE"            # 同区段作业面叠加超出道路宽度
    ACCESS = "ACCESS"          # 居民通道宽度/始终开放承诺被破坏
    OPEN_HOURS = "OPEN_HOURS"  # 封路窗口或任务允许时段被突破
    MILESTONE = "MILESTONE"    # 里程碑基线日期被突破
    PENDING_APPROVAL = "PENDING_APPROVAL"  # 任务受待审批变更影响


class DiscoveryKind(enum.Enum):
    UNKNOWN_UTILITY = "UNKNOWN_UTILITY"      # 未知管线
    PROTECTED_OBJECT = "PROTECTED_OBJECT"    # 未知保护对象/文保线索


class IncidentStatus(enum.Enum):
    OPEN = "OPEN"                        # 停工中，等待方案审批
    APPROVED_FOR_RESUME = "APPROVED_FOR_RESUME"  # 方案已批，待现场安全复工
    CLEARED = "CLEARED"                  # 已安全复工并闭环


class ProposalStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AcceptanceScope(enum.Enum):
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class ChangeKind(enum.Enum):
    TASK_UPDATE = "TASK_UPDATE"                # 工期/队伍/时段调整
    WINDOW_SHIFT = "WINDOW_SHIFT"              # 封路窗口调整
    RESUME_AFTER_DISCOVERY = "RESUME_AFTER_DISCOVERY"  # 发现未知物后的复工方案
    BASELINE_REPLAN = "BASELINE_REPLAN"        # 突破基线的整体重排


@dataclass
class Segment:
    segment_id: str
    name: str
    roadway_width_m: float


@dataclass
class Crew:
    crew_id: str
    name: str
    trade: str = ""


@dataclass
class ProtectedObject:
    object_id: str
    segment_id: str
    name: str
    radius_m: float = 0.0


@dataclass(frozen=True)
class ClosureWindow:
    window_id: str
    segment_id: str
    start_date: str  # ISO，含首尾
    end_date: str


@dataclass(frozen=True)
class Milestone:
    milestone_id: str
    name: str
    due_date: str
    task_id: str | None = None


@dataclass
class ScheduledTask:
    task_id: str
    start_date: str
    end_date: str  # 含当天
    blocked: bool = False
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Conflict:
    conflict_type: ConflictType
    message: str
    task_ids: tuple[str, ...] = ()
    segment_id: str = ""
    crew_id: str = ""
    on_date: str = ""


@dataclass
class Incident:
    incident_id: str
    task_id: str
    kind: DiscoveryKind
    note: str
    status: IncidentStatus
    reported_at: str
    proposal_id: str = ""
    cleared_at: str = ""


@dataclass
class ChangeProposal:
    proposal_id: str
    kind: ChangeKind
    reason: str
    changes: dict
    affected_tasks: list[str]
    required_level: ApprovalLevel
    status: ProposalStatus
    created_at: str
    decided_at: str = ""
    decided_by: str = ""
    decision_note: str = ""
    # 评估时排程引擎给出的预判：剩余冲突与延期天数
    impact: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptanceRecord:
    record_id: str
    task_id: str
    scope: AcceptanceScope
    quantity: float
    note: str
    recorded_at: str
    recorder: str


@dataclass
class DailyDocket:
    docket_id: str
    plan_date: str
    issued_at: str
    issued_by: str
    entries: list[dict]
    blocked: list[dict]
    sequence: int


@dataclass
class Baseline:
    baseline_id: str
    name: str
    created_at: str
    schedule: dict            # task_id -> [start, end]
    milestones: list[dict]
    milestone_values: dict    # milestone_id -> 当时预计达成日期

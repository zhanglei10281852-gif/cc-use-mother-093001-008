"""计划聚合：作业区段、工序依赖、保护对象、通行承诺、审批与验收。

聚合只负责事件应用后的状态与业务规则校验，不做排程计算
（排程在 scheduling 模块，以纯函数方式实现，便于追溯）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .events import Event


class DomainError(Exception):
    """业务规则冲突。"""


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"          # 已完成（不可再改）
    ACCEPTED = "ACCEPTED"            # 已验收（锁定）
    SUSPENDED = "SUSPENDED"          # 停工（未知管线/保护对象）
    CANCELLED = "CANCELLED"


class ChangeStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


@dataclass
class TaskState:
    task_id: str
    segment_id: str
    depends_on: tuple[str, ...]
    duration_days: int
    crew_id: str | None
    occupancy_width_m: float
    requires_closure: bool
    earliest_start: str | None
    status: TaskStatus = TaskStatus.PENDING
    version: int = 1
    suspend_reason: str | None = None
    suspend_detail: dict | None = None
    completed_at: str | None = None
    accepted_at: str | None = None
    acceptance: dict | None = None
    progress_days: int = 0  # 已完成的部分工日（部分完工）


@dataclass
class ChangeRequest:
    change_id: str
    kind: str
    payload: dict[str, Any]
    required_level: int
    status: ChangeStatus = ChangeStatus.PENDING_APPROVAL
    affected_tasks: tuple[str, ...] = ()
    conflicts: list[dict] = field(default_factory=list)
    decided_by: str | None = None
    decided_at: str | None = None
    rationale: str | None = None


class Plan:
    """施工总计划聚合根。"""

    PLAN_ID = "PLAN-1"

    def __init__(self) -> None:
        self.segments: dict[str, dict] = {}
        self.crews: dict[str, dict] = {}
        self.protections: dict[str, dict] = {}
        self.commitments: dict[str, dict] = {}
        self.closures: list[dict] = []
        self.open_windows: list[dict] = []
        self.milestones: dict[str, dict] = {}
        self.tasks: dict[str, TaskState] = {}
        self.changes: dict[str, ChangeRequest] = {}
        self.acceptances: list[dict] = []
        self.baselines: list[dict] = []
        self.daily_plans: dict[str, dict] = {}  # date -> issued plan
        self.schedule: dict[str, dict] = {}     # task_id -> {start,end}
        self.version = 0

    # ================= 事件应用 =================

    def apply(self, event: Event) -> None:
        t, p = event.type, event.payload
        if t == "PLAN_RESET":
            self.__init__()
        elif t == "SEGMENT_REGISTERED":
            self.segments[p["segment_id"]] = p
        elif t == "CREW_REGISTERED":
            self.crews[p["crew_id"]] = p
        elif t == "PROTECTION_REGISTERED":
            self.protections[p["object_id"]] = p
        elif t == "ACCESS_COMMITMENT_MADE":
            self.commitments[p["segment_id"]] = p
        elif t == "CLOSURE_WINDOW_DECLARED":
            self.closures = [c for c in self.closures
                             if not (c["segment_id"] == p["segment_id"]
                                     and c["start_date"] == p["start_date"]
                                     and c["end_date"] == p["end_date"])]
            self.closures.append(p)
        elif t == "OPEN_WINDOW_DECLARED":
            self.open_windows = [w for w in self.open_windows
                                 if not (w["segment_id"] == p["segment_id"]
                                         and w["date"] == p["date"])]
            self.open_windows.append(p)
        elif t == "MILESTONE_DEFINED":
            self.milestones[p["milestone_id"]] = p
        elif t == "TASK_REGISTERED":
            self._apply_task_registered(p)
        elif t == "TASK_UPDATED":
            self._apply_task_updated(p)
        elif t == "SCHEDULE_PUBLISHED":
            self.schedule = dict(p["assignments"])
            for tid, a in p["assignments"].items():
                task = self.tasks.get(tid)
                if task and task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.SCHEDULED
        elif t == "DAILY_PLAN_ISSUED":
            self.daily_plans[p["date"]] = p
        elif t == "BASELINE_TAKEN":
            self.baselines.append(p)
        elif t == "TASK_PROGRESSED":
            task = self.tasks[p["task_id"]]
            task.progress_days = p["progress_days"]
            task.status = TaskStatus.IN_PROGRESS
        elif t == "TASK_SUSPENDED":
            task = self.tasks[p["task_id"]]
            task.status = TaskStatus.SUSPENDED
            task.suspend_reason = p["reason"]
            task.suspend_detail = p.get("detail")
        elif t == "RESUME_REQUESTED":
            task = self.tasks[p["task_id"]]
            task.status = TaskStatus.SUSPENDED  # 仍停工，等待安全复工条件
            task.suspend_detail = {**(task.suspend_detail or {}),
                                   "resume_request": p}
        elif t == "TASK_RESUMED":
            task = self.tasks[p["task_id"]]
            prev = (task.suspend_detail or {}).get("prev_status")
            try:
                task.status = TaskStatus(prev) if prev else TaskStatus.IN_PROGRESS
            except ValueError:
                task.status = TaskStatus.IN_PROGRESS
            task.suspend_reason = None
            task.suspend_detail = None
        elif t == "TASK_PARTIALLY_COMPLETED":
            task = self.tasks[p["task_id"]]
            task.progress_days = p["progress_days"]
        elif t == "TASK_COMPLETED":
            task = self.tasks[p["task_id"]]
            task.status = TaskStatus.COMPLETED
            task.progress_days = task.duration_days
            task.completed_at = p.get("at")
        elif t == "ACCEPTANCE_RECORDED":
            task = self.tasks[p["task_id"]]
            task.status = TaskStatus.ACCEPTED
            task.accepted_at = p["recorded_at"]
            task.acceptance = p
            self.acceptances.append(p)
        elif t == "CHANGE_SUBMITTED":
            self.changes[p["change_id"]] = ChangeRequest(
                change_id=p["change_id"], kind=p["kind"], payload=p["payload"],
                required_level=p["required_level"],
                affected_tasks=tuple(p.get("affected_tasks", ())),
                conflicts=list(p.get("conflicts", [])),
            )
        elif t == "CHANGE_APPROVED":
            cr = self.changes[p["change_id"]]
            cr.status = ChangeStatus.APPROVED
            cr.decided_by = p["approver"]
            cr.decided_at = p["decided_at"]
            cr.rationale = p.get("rationale")
        elif t == "CHANGE_REJECTED":
            cr = self.changes[p["change_id"]]
            cr.status = ChangeStatus.REJECTED
            cr.decided_by = p["approver"]
            cr.decided_at = p["decided_at"]
            cr.rationale = p.get("rationale")
        elif t == "CHANGE_APPLIED":
            self.changes[p["change_id"]].status = ChangeStatus.APPLIED
        # 未知事件忽略，保证向前兼容

    def _apply_task_registered(self, p: dict) -> None:
        existing = self.tasks.get(p["task_id"])
        status = TaskStatus.PENDING
        if existing:
            status = existing.status
        self.tasks[p["task_id"]] = TaskState(
            task_id=p["task_id"], segment_id=p["segment_id"],
            depends_on=tuple(p["depends_on"]), duration_days=p["duration_days"],
            crew_id=p.get("crew_id"), occupancy_width_m=p.get("occupancy_width_m", 0.0),
            requires_closure=p.get("requires_closure", False),
            earliest_start=p.get("earliest_start"),
            status=status, progress_days=(existing.progress_days if existing else 0),
            acceptance=(existing.acceptance if existing else None),
            accepted_at=(existing.accepted_at if existing else None),
            completed_at=(existing.completed_at if existing else None),
        )

    def _apply_task_updated(self, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        for key in ("segment_id", "duration_days", "crew_id",
                    "occupancy_width_m", "requires_closure", "earliest_start"):
            if key in p:
                setattr(task, key, p[key])
        if "depends_on" in p:
            task.depends_on = tuple(p["depends_on"])
        task.version += 1

    # ================= 业务规则 =================

    def require_task(self, task_id: str) -> TaskState:
        if task_id not in self.tasks:
            raise DomainError(f"任务不存在: {task_id}")
        return self.tasks[task_id]

    def require_segment(self, segment_id: str) -> dict:
        if segment_id not in self.segments:
            raise DomainError(f"区段不存在: {segment_id}")
        return self.segments[segment_id]

    def assert_task_mutable(self, task: TaskState) -> None:
        if task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
            raise DomainError(
                f"任务 {task.task_id} 已{task.status.value}，验收/完工记录锁定不可改动")

    def assert_can_update(self, task: TaskState) -> None:
        self.assert_task_mutable(task)
        if task.status == TaskStatus.SUSPENDED:
            raise DomainError(f"任务 {task.task_id} 停工中，须先走复工或变更流程")

    def assert_no_cycle(self, task_id: str, depends_on: tuple[str, ...]) -> None:
        stack, seen = list(depends_on), set()
        while stack:
            cur = stack.pop()
            if cur == task_id:
                raise DomainError(f"依赖成环: {task_id}")
            if cur in seen:
                continue
            seen.add(cur)
            t = self.tasks.get(cur)
            if t:
                stack.extend(t.depends_on)

    def assert_dependencies_exist(self, depends_on: tuple[str, ...]) -> None:
        missing = [d for d in depends_on if d not in self.tasks]
        if missing:
            raise DomainError(f"依赖任务不存在: {missing}")

    def assert_can_complete(self, task: TaskState) -> None:
        if task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
            raise DomainError(f"任务 {task.task_id} 已完工，不可重复完工")
        if task.status == TaskStatus.SUSPENDED:
            raise DomainError(f"任务 {task.task_id} 停工中，不能完工")
        for dep_id in task.depends_on:
            dep = self.tasks[dep_id]
            if dep.status not in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
                raise DomainError(f"前置任务 {dep_id} 未完工，{task.task_id} 不能完工")

    def assert_can_accept(self, task: TaskState) -> None:
        if task.status == TaskStatus.ACCEPTED:
            raise DomainError(f"任务 {task.task_id} 已有验收记录，禁止覆盖")
        if task.status != TaskStatus.COMPLETED:
            raise DomainError(f"任务 {task.task_id} 未完工，不能验收")

    def open_blocked_dates(self, segment_id: str) -> set[str]:
        return {w["date"] for w in self.open_windows
                if w["segment_id"] == segment_id and not w["work_allowed"]}

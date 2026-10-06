"""排程引擎：拓扑正排 + 资源/空间/通行/开放时段/里程碑冲突识别 + 延期传播。

仅负责“给定计划快照算出结果”，不持有状态；状态由 service/repository 管理。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import timeutil
from .contracts import AccessCommitment, WorkTask
from .models import (
    ClosureWindow,
    Conflict,
    ConflictType,
    Milestone,
    ProtectedObject,
    ScheduledTask,
)


@dataclass
class Plan:
    tasks: dict[str, WorkTask]
    segments: dict = field(default_factory=dict)
    crews: dict = field(default_factory=dict)
    protected_objects: list[ProtectedObject] = field(default_factory=list)
    windows: list[ClosureWindow] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)
    access: dict[str, AccessCommitment] = field(default_factory=dict)
    project_start: str = ""
    # 运行态输入
    stopped: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    # 已完工任务的实际完工日，使其后继任务不会排到该日之前
    completion_dates: dict[str, str] = field(default_factory=dict)
    # 计划日历日（as-of）：新正排的任务不会早于该日
    as_of: str = ""
    remaining_days: dict[str, int] = field(default_factory=dict)
    held_for_approval: set[str] = field(default_factory=set)
    # 任务级最早开工地板（已在执行任务的复工日等）
    start_floors: dict[str, str] = field(default_factory=dict)


@dataclass
class ScheduleResult:
    tasks: dict[str, ScheduledTask]
    order: list[str]
    conflicts: list[Conflict]
    # segment -> date -> 当日剩余可通行宽度（米）
    residual_width: dict[str, dict[str, float]]
    blocked: set[str]

    @property
    def feasible(self) -> bool:
        return not self.conflicts

    def conflicts_of(self, conflict_type: ConflictType) -> list[Conflict]:
        return [c for c in self.conflicts if c.conflict_type is conflict_type]


def _topo_order(tasks: dict[str, WorkTask]) -> list[str]:
    visited: dict[str, int] = {}  # 0 访问过 1 在栈上 2 完成
    order: list[str] = []

    def visit(tid: str, chain: tuple[str, ...]) -> None:
        state = visited.get(tid, 0)
        if state == 2:
            return
        if state == 1:
            cycle = " -> ".join(chain[chain.index(tid):] + (tid,))
            raise ValueError(f"工序依赖存在环: {cycle}")
        visited[tid] = 1
        task = tasks.get(tid)
        if task is None:
            raise ValueError(f"依赖了不存在的任务: {tid}")
        for dep in task.depends_on:
            visit(dep, chain + (tid,))
        visited[tid] = 2
        order.append(tid)

    for tid in tasks:
        visit(tid, ())
    return order


def _effective_duration(plan: Plan, tid: str) -> int:
    return max(1, plan.remaining_days.get(tid, plan.tasks[tid].duration_days))


def _forward(plan: Plan):
    """正排，返回 (scheduled, blocked, blocked_by_map)。停工任务及其下游全部阻断。"""
    order = _topo_order(plan.tasks)
    scheduled: dict[str, ScheduledTask] = {}
    blocked: set[str] = set()
    blocked_by: dict[str, list[str]] = {}

    for tid in order:
        task = plan.tasks[tid]
        if tid in plan.completed:
            # 已完工验收：依赖视为满足，不再参与资源/空间占用；
            # 若有实际完工日，则作为后继任务的最早开工锚点。
            done_day = plan.completion_dates.get(tid, "")
            scheduled[tid] = ScheduledTask(tid, done_day, done_day)
            continue
        blockers = [d for d in task.depends_on if d in blocked]
        if tid in plan.stopped or blockers:
            blocked.add(tid)
            reasons = sorted(set(([tid] if tid in plan.stopped else []) + blockers))
            blocked_by[tid] = reasons
            scheduled[tid] = ScheduledTask(tid, "", "", blocked=True, blocked_by=reasons)
            continue

        start = plan.project_start or task.earliest_start or ""
        floor_candidates = [task.earliest_start, plan.start_floors.get(tid)]
        if plan.as_of:
            floor_candidates.append(plan.as_of)
        for dep in task.depends_on:
            dep_end = scheduled[dep].end_date
            if dep_end:
                floor_candidates.append(timeutil.add_days(dep_end, 1))
        for floor in floor_candidates:
            if floor and floor > start:
                start = floor
        end = timeutil.add_days(start, _effective_duration(plan, tid) - 1)
        scheduled[tid] = ScheduledTask(tid, start, end)

    return scheduled, blocked, blocked_by, order


def _windows_by_segment(plan: Plan) -> dict[str, list[ClosureWindow]]:
    out: dict[str, list[ClosureWindow]] = {}
    for w in plan.windows:
        out.setdefault(w.segment_id, []).append(w)
    return out


def _detect_conflicts(plan: Plan, scheduled: dict[str, ScheduledTask],
                      blocked: set[str]) -> tuple[list[Conflict], dict]:
    conflicts: list[Conflict] = []
    windows = _windows_by_segment(plan)

    active = [t for t in plan.tasks if t not in blocked and t not in plan.completed]
    # 待审批变更挂起的任务暂不实际占用资源/空间，但仍参与正排以便评估。
    working = [t for t in active if t not in plan.held_for_approval]

    # 预计算每个任务每天占用的沿街宽度（封路=全路宽）
    def occupy(task: WorkTask, day: str) -> float:
        seg = plan.segments.get(task.segment_id)
        width = seg.roadway_width_m if seg else 0.0
        return width if task.requires_closure else task.occupies_width_m

    # RESOURCE：同一施工队同日多任务
    crew_days: dict[tuple[str, str], list[str]] = {}
    # SPACE / ACCESS：同区段同日占用合计
    seg_days: dict[tuple[str, str], list[str]] = {}
    for tid in working:
        task = plan.tasks[tid]
        sch = scheduled[tid]
        for day in timeutil.iter_dates(sch.start_date, sch.end_date):
            if task.crew_id:
                crew_days.setdefault((task.crew_id, day), []).append(tid)
            seg_days.setdefault((task.segment_id, day), []).append(tid)

    for (crew_id, day), tids in crew_days.items():
        if len(set(tids)) > 1:
            conflicts.append(Conflict(
                ConflictType.RESOURCE,
                f"施工队 {crew_id} 在 {day} 被同时派工: {', '.join(sorted(set(tids)))}",
                tuple(sorted(set(tids))), crew_id=crew_id, on_date=day))

    residual: dict[str, dict[str, float]] = {}
    for (seg_id, day), tids in seg_days.items():
        seg = plan.segments.get(seg_id)
        road = seg.roadway_width_m if seg else 0.0
        total = sum(occupy(plan.tasks[t], day) for t in set(tids))
        commitment = plan.access.get(seg_id)
        closure_today = any(plan.tasks[t].requires_closure for t in set(tids))
        # 当日是否处于经批准的封路窗口
        window_covers = any(w.start_date <= day <= w.end_date
                            for w in windows.get(seg_id, []))
        remain = max(0.0, road - total)
        residual.setdefault(seg_id, {})[day] = remain

        if total > road + 1e-9:
            conflicts.append(Conflict(
                ConflictType.SPACE,
                f"{seg_id} 在 {day} 作业面占用 {total:.1f}m 超过路宽 {road:.1f}m",
                tuple(sorted(set(tids))), segment_id=seg_id, on_date=day))
        # 封路窗口内允许全断面，豁免最小余宽；窗口外封路在 OPEN_HOURS 中另行报错
        width_exempt = window_covers and closure_today
        if commitment and not width_exempt and \
                remain + 1e-9 < commitment.minimum_width_m:
            conflicts.append(Conflict(
                ConflictType.ACCESS,
                f"{seg_id} 在 {day} 居民通道余宽 {remain:.1f}m 低于承诺 "
                f"{commitment.minimum_width_m:.1f}m",
                tuple(sorted(set(tids))), segment_id=seg_id, on_date=day))
        if commitment and commitment.always_open and closure_today:
            conflicts.append(Conflict(
                ConflictType.ACCESS,
                f"{seg_id} 承诺始终开放，但 {day} 存在全断面封路作业",
                tuple(sorted(set(tids))), segment_id=seg_id, on_date=day))

    # OPEN_HOURS：封路作业必须落在窗口内；最晚完工约束
    for tid in working:
        task = plan.tasks[tid]
        sch = scheduled[tid]
        if task.requires_closure:
            for day in timeutil.iter_dates(sch.start_date, sch.end_date):
                inside = any(w.start_date <= day <= w.end_date
                             for w in windows.get(task.segment_id, []))
                if not inside:
                    conflicts.append(Conflict(
                        ConflictType.OPEN_HOURS,
                        f"任务 {tid} 在 {day} 需要封路但不在该区段封路窗口内",
                        (tid,), segment_id=task.segment_id, on_date=day))
        if task.latest_finish and sch.end_date > task.latest_finish:
            conflicts.append(Conflict(
                ConflictType.OPEN_HOURS,
                f"任务 {tid} 预计 {sch.end_date} 完工，晚于最晚完工日 "
                f"{task.latest_finish}", (tid,), on_date=sch.end_date))

    # MILESTONE
    for ms in plan.milestones:
        if ms.task_id and ms.task_id in scheduled and ms.task_id not in blocked:
            end = scheduled[ms.task_id].end_date
            if end and end > ms.due_date:
                conflicts.append(Conflict(
                    ConflictType.MILESTONE,
                    f"里程碑 {ms.name}({ms.milestone_id}) 基线 {ms.due_date}，"
                    f"预计 {end} 达成，延期 {timeutil.days_between(ms.due_date, end)} 天",
                    (ms.task_id,), on_date=end))

    # 待审批变更挂起
    for tid in sorted(plan.held_for_approval):
        if tid in plan.tasks:
            conflicts.append(Conflict(
                ConflictType.PENDING_APPROVAL,
                f"任务 {tid} 的变更方案待批，暂不可签发/复工", (tid,)))

    return conflicts, residual


def schedule(plan: Plan) -> ScheduleResult:
    scheduled, blocked, _blocked_by, order = _forward(plan)
    conflicts, residual = _detect_conflicts(plan, scheduled, blocked)
    return ScheduleResult(scheduled, order, conflicts, residual, blocked)


def verify_access_commitment(plan: Plan, result: ScheduleResult | None = None) -> dict:
    """逐日核验居民通道承诺，返回每个承诺的最坏余宽与违约日期。"""
    result = result or schedule(plan)
    windows = _windows_by_segment(plan)
    report = {}
    for seg_id, commitment in plan.access.items():
        widths = result.residual_width.get(seg_id, {})
        if not widths:
            report[seg_id] = {"satisfied": True, "worst_width_m": None,
                              "breach_dates": [], "minimum_width_m":
                              commitment.minimum_width_m, "always_open":
                              commitment.always_open, "checked": False}
            continue

        # 当日有封路作业且落在批准窗口内 → 余宽豁免
        closure_days = set()
        for tid in plan.tasks:
            task = plan.tasks[tid]
            if task.segment_id != seg_id or not task.requires_closure:
                continue
            sch = result.tasks.get(tid)
            if sch and not sch.blocked and sch.start_date:
                closure_days.update(timeutil.iter_dates(sch.start_date, sch.end_date))
        exempt_days = {d for d in closure_days
                       if any(w.start_date <= d <= w.end_date
                              for w in windows.get(seg_id, []))}
        considered = {d: w for d, w in widths.items() if d not in exempt_days}
        # 全部作业日都在封路窗口内：余宽逐日豁免，用全量宽度仅作展示
        if not considered:
            considered = dict(widths)
        worst_day = min(considered, key=lambda d: considered[d])
        breach = sorted(d for d, w in considered.items()
                        if w + 1e-9 < commitment.minimum_width_m)
        # always_open 的违约日（即使在窗口内也不允许封路）
        closure_breach = []
        if commitment.always_open:
            closure_breach = [c.on_date for c in result.conflicts
                              if c.conflict_type is ConflictType.ACCESS
                              and c.segment_id == seg_id and "始终开放" in c.message]
        report[seg_id] = {
            "satisfied": not breach and not closure_breach,
            "worst_width_m": round(considered[worst_day], 2),
            "worst_date": worst_day,
            "breach_dates": sorted(set(breach + closure_breach)),
            "exempt_closure_dates": sorted(exempt_days),
            "minimum_width_m": commitment.minimum_width_m,
            "always_open": commitment.always_open,
            "checked": True,
        }
    return report


def trace_delay(plan: Plan, task_id: str, delay_days: int) -> dict:
    """模拟任务 task_id 延误 delay_days 的传播：下游顺移、里程碑突破、承诺违约。

    通过给任务增加剩余工期实现延误（任务未完工时的语义）。
    """
    if task_id not in plan.tasks:
        raise ValueError(f"任务不存在: {task_id}")
    if delay_days < 0:
        raise ValueError("延误天数不能为负")

    base = schedule(plan)
    perturbed = Plan(
        tasks=dict(plan.tasks), segments=plan.segments, crews=plan.crews,
        protected_objects=plan.protected_objects, windows=plan.windows,
        milestones=plan.milestones, access=plan.access,
        project_start=plan.project_start, stopped=set(plan.stopped),
        completed=set(plan.completed),
        completion_dates=dict(plan.completion_dates), as_of=plan.as_of,
        remaining_days=dict(plan.remaining_days),
        held_for_approval=set(plan.held_for_approval),
        start_floors=dict(plan.start_floors))
    perturbed.remaining_days[task_id] = _effective_duration(plan, task_id) + delay_days
    after = schedule(perturbed)

    propagation = []
    for tid in after.order:
        b, a = base.tasks[tid], after.tasks[tid]
        if a.blocked:
            continue
        shift = timeutil.days_between(b.end_date, a.end_date) if b.end_date and a.end_date else 0
        if shift > 0 and tid != task_id:
            propagation.append({"task_id": tid, "shift_days": shift,
                                "old_end": b.end_date, "new_end": a.end_date})
    propagation.sort(key=lambda x: (-x["shift_days"], x["task_id"]))

    milestone_hits = []
    for ms in plan.milestones:
        if ms.task_id and not after.tasks[ms.task_id].blocked:
            new_end = after.tasks[ms.task_id].end_date
            old_end = base.tasks[ms.task_id].end_date
            if new_end > ms.due_date:
                milestone_hits.append({
                    "milestone_id": ms.milestone_id, "name": ms.name,
                    "due_date": ms.due_date, "projected": new_end,
                    "late_days": timeutil.days_between(ms.due_date, new_end),
                    "was_already_late": old_end > ms.due_date})

    base_access = verify_access_commitment(plan, base)
    after_access = verify_access_commitment(perturbed, after)
    access_breached = sorted(
        seg for seg, rep in after_access.items()
        if rep["satisfied"] is False and base_access.get(seg, {}).get("satisfied", True))

    old_conf = {(c.conflict_type, c.on_date, c.segment_id, c.crew_id)
                for c in base.conflicts}
    new_conflicts = [c for c in after.conflicts
                     if (c.conflict_type, c.on_date, c.segment_id, c.crew_id) not in old_conf]

    return {
        "origin_task": task_id,
        "delay_days": delay_days,
        "origin_old_end": base.tasks[task_id].end_date,
        "origin_new_end": after.tasks[task_id].end_date,
        "propagation": propagation,
        "milestones_hit": milestone_hits,
        "access_newly_breached": access_breached,
        "new_conflicts": [
            {"type": c.conflict_type.value, "message": c.message,
             "on_date": c.on_date, "segment_id": c.segment_id}
            for c in new_conflicts],
        "feasible_after": after.feasible,
    }

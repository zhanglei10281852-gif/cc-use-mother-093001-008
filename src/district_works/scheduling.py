"""纯函数排程内核：冲突识别、前向重排、延期传播、通行校核。

不依赖事件存储，输入为 Plan 快照，输出完全确定，便于命令行/接口追溯。

时间粒度为"工作日"（日历日）；区段登记的开放时段约束
（OpenWindow.work_allowed=False）的日期自动跳过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .domain import Plan, TaskState, TaskStatus

ISO = "%Y-%m-%d"


def parse_d(s: str) -> date:
    return date.fromisoformat(s)


def fmt_d(d: date) -> str:
    return d.isoformat()


# ================= 日历 =================

def daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def working_days_for(plan: Plan, segment_id: str, start: date,
                     horizon_days: int) -> list[date]:
    blocked = plan.open_blocked_dates(segment_id)
    return [d for d in daterange(start, horizon_days)
            if fmt_d(d) not in blocked]


def closure_covers(plan: Plan, segment_id: str, d: date) -> bool:
    for w in plan.closures:
        if w["segment_id"] != segment_id:
            continue
        if parse_d(w["start_date"]) <= d <= parse_d(w["end_date"]):
            return True
    return False


# ================= 静态约束校核 =================

def static_conflicts(plan: Plan) -> list[dict]:
    """与排程无关、永远成立的硬冲突。"""
    out: list[dict] = []
    for task in plan.tasks.values():
        if task.status in (TaskStatus.CANCELLED, TaskStatus.ACCEPTED):
            continue
        seg = plan.segments.get(task.segment_id)
        if not seg:
            out.append({"type": "missing_segment", "task_id": task.task_id,
                        "segment_id": task.segment_id})
            continue
        commit = plan.commitments.get(task.segment_id)
        if commit and task.occupancy_width_m > 0:
            free_width = seg["width_m"] - commit["minimum_width_m"]
            if task.occupancy_width_m > free_width + 1e-9:
                out.append({
                    "type": "access_width",
                    "task_id": task.task_id,
                    "segment_id": task.segment_id,
                    "occupancy_width_m": task.occupancy_width_m,
                    "available_width_m": round(free_width, 3),
                    "message": ("作业占用宽度超过承诺通行后的可用宽度，"
                                f"需≤{free_width:.2f}m，实际{task.occupancy_width_m:.2f}m"),
                })
        # 文保控制线：区段在控制线内时，触碰保护对象需对应级别（在变更流程复核）
    return out


def _zones_of(plan: Plan, segment_id: str) -> set[str]:
    seg = plan.segments.get(segment_id)
    if not seg:
        return set()
    zones = set(seg.get("zone_ids") or ())
    zones.add(f"SEG:{segment_id}")  # 同一区段天然互斥
    return zones


# ================= 前向排程 =================

@dataclass
class ScheduleResult:
    assignments: dict[str, dict] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"assignments": self.assignments,
                "conflicts": self.conflicts, "order": self.order}


def _topological_order(tasks: dict[str, TaskState]) -> list[str]:
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(tid: str) -> None:
        if tid in visited or tid not in tasks:
            return
        if tid in visiting:  # 理论上注册时已拦环，防御性处理
            raise ValueError(f"依赖成环: {tid}")
        visiting.add(tid)
        for dep in tasks[tid].depends_on:
            visit(dep)
        visiting.discard(tid)
        visited.add(tid)
        ordered.append(tid)

    for tid in sorted(tasks):
        visit(tid)
    return ordered


def compute_schedule(plan: Plan, start_date: str,
                     horizon_days: int = 1095,
                     overrides: dict[str, dict] | None = None,
                     lock_existing: bool = True) -> ScheduleResult:
    """前向重排。

    overrides: 场景假设，如 {"W-3": {"delay_until": "2026-05-10"}} 或
               {"W-3": {"duration_days": 6}}，仅用于推演，不改动计划。
    lock_existing: 已有发布排期的完工/在施任务保持原日期（用于传播分析时
               对比基线）；缺省 True。
    """
    overrides = overrides or {}
    result = ScheduleResult()
    result.conflicts.extend(static_conflicts(plan))

    horizon_start = parse_d(start_date)
    cal = {sid: working_days_for(plan, sid, horizon_start, horizon_days)
           for sid in plan.segments}

    crew_busy: dict[str, set[date]] = {}
    zone_busy: dict[str, set[date]] = {}

    active = {tid: t for tid, t in plan.tasks.items()
              if t.status not in (TaskStatus.CANCELLED,)}

    # 已发布排期的已完工/验收任务作为固定锚点占用资源
    def take(task: TaskState, days: list[date]) -> None:
        if task.crew_id:
            crew_busy.setdefault(task.crew_id, set()).update(days)
        for z in _zones_of(plan, task.segment_id):
            zone_busy.setdefault(z, set()).update(days)

    order = _topological_order(active)
    result.order = order

    for tid in order:
        task = active[tid]
        ovr = overrides.get(tid, {})
        duration = int(ovr.get("duration_days", task.duration_days))
        progress = task.progress_days if "duration_days" not in ovr else 0
        if task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
            a = plan.schedule.get(tid)
            if a:
                days = [parse_d(s) for s in a["days"]]
                result.assignments[tid] = dict(a)
                take(task, days)
            continue

        remaining = max(1, duration - progress)

        # 已锁定的在施任务沿用发布日期
        if lock_existing and tid in plan.schedule and task.status in (
                TaskStatus.IN_PROGRESS, TaskStatus.SCHEDULED) and not ovr:
            a = plan.schedule[tid]
            days = [parse_d(s) for s in a["days"]]
            result.assignments[tid] = dict(a)
            take(task, days)
            continue

        earliest = horizon_start
        blocked_by_dep: str | None = None
        if task.earliest_start:
            earliest = max(earliest, parse_d(task.earliest_start))
        if "delay_until" in ovr:
            earliest = max(earliest, parse_d(ovr["delay_until"]))
        if "start_no_earlier_than" in ovr:
            earliest = max(earliest, parse_d(ovr["start_no_earlier_than"]))
        for dep_id in task.depends_on:
            dep_a = result.assignments.get(dep_id)
            dep_task = active.get(dep_id)
            if dep_a:
                earliest = max(earliest, parse_d(dep_a["end"]) + timedelta(days=1))
            elif dep_task is not None:
                # 前置存在但本轮排不下 → 依赖链断裂，本任务不可排
                blocked_by_dep = dep_id

        if blocked_by_dep:
            result.conflicts.append({
                "type": "dependency_blocked",
                "task_id": tid,
                "segment_id": task.segment_id,
                "blocked_by": blocked_by_dep,
                "message": f"任务 {tid} 的前置 {blocked_by_dep} 无法排入，"
                           "依赖链断裂，本任务同样无法排程",
            })
            continue

        days = plan.segments.get(task.segment_id) and cal[task.segment_id] or []
        chosen = _find_slot(
            plan=plan, task=task, days=days, earliest=earliest,
            remaining=remaining, crew_busy=crew_busy, zone_busy=zone_busy)

        if chosen is None:
            result.conflicts.append({
                "type": "unschedulable",
                "task_id": tid,
                "segment_id": task.segment_id,
                "after": fmt_d(earliest),
                "remaining_days": remaining,
                "message": f"任务 {tid} 在视窗内找不到同时满足依赖、资源、空间"
                           f"与开放时段的连续{remaining}个工作日",
            })
            continue

        assignment = {"start": fmt_d(chosen[0]), "end": fmt_d(chosen[-1]),
                      "days": [fmt_d(d) for d in chosen],
                      "crew_id": task.crew_id,
                      "segment_id": task.segment_id}
        result.assignments[tid] = assignment
        take(task, chosen)

    # 封路窗口容量复核（无法落入窗口的情形已在选位中体现，这里给汇总）
    result.conflicts.extend(_closure_gap_conflicts(plan, result.assignments))
    return result


def _find_slot(*, plan: Plan, task: TaskState, days: list[date],
               earliest: date, remaining: int,
               crew_busy: dict[str, set[date]],
               zone_busy: dict[str, set[date]]) -> list[date] | None:
    zones = _zones_of(plan, task.segment_id)
    crew = task.crew_id
    candidates: list[date] = []
    for d in days:
        if d < earliest:
            continue
        if task.requires_closure and not closure_covers(plan, task.segment_id, d):
            candidates = []  # 封路任务必须整体处于窗口内
            continue
        if crew and d in crew_busy.get(crew, set()):
            candidates = []
            continue
        if any(d in zone_busy.get(z, set()) for z in zones):
            candidates = []
            continue
        candidates.append(d)
        if len(candidates) == remaining:
            return list(candidates)
    return None


def _closure_gap_conflicts(plan: Plan,
                           assignments: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for tid, a in assignments.items():
        task = plan.tasks[tid]
        if not task.requires_closure:
            continue
        uncovered = [s for s in a["days"]
                     if not closure_covers(plan, task.segment_id, parse_d(s))]
        if uncovered:
            out.append({"type": "closure_window",
                        "task_id": tid, "segment_id": task.segment_id,
                        "dates_outside_window": uncovered,
                        "message": f"封路任务 {tid} 有{len(uncovered)}个作业日落入"
                                   "批准封路窗口之外，封路窗口将失效"})
    return out


# ================= 延期传播追溯 =================

def trace_delay(plan: Plan, start_date: str, task_id: str,
                delay_days: int | None = None,
                duration_override: int | None = None,
                horizon_days: int = 1095) -> dict:
    """推演某任务延期/工期变化如何沿依赖与资源链传播。

    返回 baseline / scenario 两套日期与逐任务传播链。
    """
    if task_id not in plan.tasks:
        raise KeyError(f"任务不存在: {task_id}")
    base = compute_schedule(plan, start_date, horizon_days)

    ovr: dict[str, dict] = {}
    base_a = base.assignments.get(task_id)
    triggers: list[str] = []
    if duration_override is not None:
        ovr[task_id] = {"duration_days": duration_override}
        triggers.append(f"工期改为 {duration_override} 天")
    if delay_days is not None:
        anchor = (base_a["start"] if base_a else start_date)
        new_earliest = parse_d(anchor) + timedelta(days=delay_days)
        ovr[task_id] = {**ovr.get(task_id, {}),
                        "start_no_earlier_than": fmt_d(new_earliest)}
        triggers.append(f"最早开工推迟 {delay_days} 天至 {fmt_d(new_earliest)}")

    # 传播分析时不锁定既有排期，让所有下游任务自由前向重排
    scenario = compute_schedule(plan, start_date, horizon_days,
                                overrides=ovr, lock_existing=False)

    chain: list[dict] = []
    root_conflict = next((c for c in scenario.conflicts
                          if c.get("task_id") == task_id), None)
    root_b = base.assignments.get(task_id)
    root_s = scenario.assignments.get(task_id)
    chain.append({
        "task_id": task_id,
        "segment_id": plan.tasks[task_id].segment_id,
        "baseline_start": root_b["start"] if root_b else None,
        "scenario_start": root_s["start"] if root_s else None,
        "baseline_end": root_b["end"] if root_b else None,
        "scenario_end": root_s["end"] if root_s else None,
        "shift_days": ((parse_d(root_s["start"]) - parse_d(root_b["start"])).days
                       if root_b and root_s else 0),
        "root": True,
        "unschedulable": root_s is None,
        "blocking_conflict": root_conflict,
        "causes": ["变更直接作用任务"]
                  + ([root_conflict["message"]] if root_conflict else []),
    })
    for tid in scenario.order:
        if tid == task_id:
            continue
        b, s = base.assignments.get(tid), scenario.assignments.get(tid)
        if not s or not b:
            # 触发任务排不下导致的级联不可排，单独标记，不当作"提前"
            c = next((x for x in scenario.conflicts
                      if x.get("task_id") == tid), None)
            if c:
                chain.append({"task_id": tid,
                              "segment_id": plan.tasks[tid].segment_id,
                              "baseline_start": b["start"] if b else None,
                              "scenario_start": None,
                              "baseline_end": b["end"] if b else None,
                              "scenario_end": None, "shift_days": 0,
                              "root": False, "unschedulable": True,
                              "blocking_conflict": c,
                              "causes": [f"上游 {task_id} 无法排入"
                                         f"（见 {c['type']}）"]})
            continue
        shift = (parse_d(s["start"]) - parse_d(b["start"])).days
        # 延期追溯只报告真正的顺延；任务因重排而提前属于噪声
        if shift > 0:
            causes = _causes_for(plan, tid, scenario, base, ovr)
            chain.append({
                "task_id": tid,
                "segment_id": plan.tasks[tid].segment_id,
                "baseline_start": b["start"],
                "scenario_start": s["start"],
                "baseline_end": b["end"], "scenario_end": s["end"],
                "shift_days": shift, "root": False, "unschedulable": False,
                "causes": causes,
            })

    milestone_impact = _milestone_impact(plan, scenario.assignments)
    return {
        "trigger_task": task_id,
        "triggers": triggers,
        "propagation_chain": chain,
        "milestone_impact": milestone_impact,
        "baseline": base.as_dict(),
        "scenario": scenario.as_dict(),
    }


def _causes_for(plan: Plan, tid: str, scenario: ScheduleResult,
                base: ScheduleResult, ovr: dict) -> list[str]:
    task = plan.tasks[tid]
    causes: list[str] = []
    s = scenario.assignments[tid]
    s_start = parse_d(s["start"])
    # 依赖传导
    for dep_id in task.depends_on:
        dep_s = scenario.assignments.get(dep_id)
        dep_b = base.assignments.get(dep_id)
        if dep_s and dep_b and dep_s["end"] != dep_b["end"]:
            causes.append(f"前置 {dep_id} 完工日 {dep_b['end']} → {dep_s['end']}")
        elif dep_s and parse_d(dep_s["end"]) >= s_start:
            causes.append(f"等待前置 {dep_id} 完工({dep_s['end']})")
    # 资源/空间传导：开工日被同班组或同空间任务占用
    day_before = s["days"][0]
    for other_id, other_a in scenario.assignments.items():
        if other_id == tid or day_before not in other_a["days"]:
            continue
        other = plan.tasks.get(other_id)
        if not other:
            continue
        if other.crew_id and other.crew_id == task.crew_id:
            causes.append(f"班组 {task.crew_id} 当日在 {other_id}({other.segment_id})")
        if _zones_of(plan, other.segment_id) & _zones_of(plan, task.segment_id):
            causes.append(f"空间冲突：{other_id} 占用同一作业空间/区段")
    if tid in ovr:
        causes.append("变更直接作用任务")
    return sorted(set(causes))


def _milestone_impact(plan: Plan, assignments: dict[str, dict]) -> list[dict]:
    out = []
    for ms in plan.milestones.values():
        due = parse_d(ms["due_date"])
        latest_end: date | None = None
        missing = []
        for tid in ms["task_ids"]:
            a = assignments.get(tid)
            if not a:
                missing.append(tid)
                continue
            end = parse_d(a["end"])
            latest_end = end if latest_end is None else max(latest_end, end)
        breach = latest_end is not None and latest_end > due
        out.append({"milestone_id": ms["milestone_id"], "name": ms["name"],
                    "due_date": ms["due_date"],
                    "forecast_end": fmt_d(latest_end) if latest_end else None,
                    "breached": breach,
                    "slack_days": (due - latest_end).days if latest_end else None,
                    "unscheduled": missing})
    return out


# ================= 通行承诺校核 =================

def verify_access(plan: Plan, assignments: dict[str, dict] | None = None,
                  through_date: str | None = None) -> dict:
    """逐日校核居民/商户通行承诺是否始终满足。

    - always_open 承诺：当日该区段不得有 requires_closure 的作业；
    - 最小宽度：当日在施任务占用宽度后，剩余宽度 ≥ 承诺最小宽度。
    """
    assignments = assignments if assignments is not None else plan.schedule
    violations: list[dict] = []
    days_checked: set[str] = set()

    by_segment_day: dict[tuple[str, str], list[str]] = {}
    for tid, a in assignments.items():
        task = plan.tasks.get(tid)
        if not task or task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
            # 已完工任务不再占道
            continue
        for day in a["days"]:
            if through_date and day > through_date:
                continue
            by_segment_day.setdefault((task.segment_id, day), []).append(tid)
            days_checked.add(day)

    for (seg_id, day), tids in sorted(by_segment_day.items()):
        commit = plan.commitments.get(seg_id)
        if not commit:
            continue
        seg = plan.segments[seg_id]
        occupancy = sum(plan.tasks[t].occupancy_width_m for t in tids)
        free_width = seg["width_m"] - occupancy
        if free_width + 1e-9 < commit["minimum_width_m"]:
            violations.append({
                "date": day, "segment_id": seg_id, "task_ids": tids,
                "rule": "minimum_width",
                "free_width_m": round(free_width, 3),
                "required_width_m": commit["minimum_width_m"],
                "message": f"{day} 区段{seg_id}剩余通行宽度{free_width:.2f}m"
                           f"<承诺{commit['minimum_width_m']:.2f}m",
            })
        if commit["always_open"]:
            closure_tasks = [t for t in tids
                             if plan.tasks[t].requires_closure]
            if closure_tasks:
                violations.append({
                    "date": day, "segment_id": seg_id,
                    "task_ids": closure_tasks, "rule": "always_open",
                    "message": f"{day} 区段{seg_id}承诺始终开放，但封路作业"
                               f"{closure_tasks} 占用",
                })

    return {
        "satisfied": not violations,
        "days_checked": sorted(days_checked),
        "violations": violations,
        "commitments": [
            {"segment_id": c["segment_id"],
             "minimum_width_m": c["minimum_width_m"],
             "always_open": c["always_open"]}
            for c in plan.commitments.values()],
    }


# ================= 日计划 =================

def build_daily_plan(plan: Plan, day: str,
                     assignments: dict[str, dict] | None = None) -> dict:
    assignments = assignments if assignments is not None else plan.schedule
    active = []
    for tid, a in assignments.items():
        if day not in a["days"]:
            continue
        task = plan.tasks[tid]
        active.append({
            "task_id": tid, "segment_id": task.segment_id,
            "crew_id": task.crew_id,
            "status": task.status.value,
            "requires_closure": task.requires_closure,
            "occupancy_width_m": task.occupancy_width_m,
            "suspended": task.status == TaskStatus.SUSPENDED,
        })
    crews = sorted({x["crew_id"] for x in active if x["crew_id"]})
    segments = sorted({x["segment_id"] for x in active})
    access = verify_access(plan, assignments, through_date=day)
    blocked = [w for w in plan.open_windows
               if w["date"] == day and not w["work_allowed"]]
    return {
        "date": day, "tasks": active, "crews": crews, "segments": segments,
        "open_window_blocks": blocked,
        "access_violations": [v for v in access["violations"]
                              if v["date"] == day],
        "access_satisfied": all(v["date"] != day
                                for v in access["violations"]),
    }

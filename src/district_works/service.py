"""应用服务：命令处理、幂等、变更控制、安全复工、查询追溯。

所有命令携带 command_id；重复提交返回首次回执（重启后仍成立）。
状态由事件重放构建，SUSPENDED / PENDING_APPROVAL 等状态随服务重启恢复。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .domain import (ChangeRequest, ChangeStatus, DomainError, Plan,
                     TaskState, TaskStatus)
from .events import Event
from .scheduling import (build_daily_plan, compute_schedule, fmt_d,
                         parse_d, static_conflicts, trace_delay, verify_access)
from .store import EventStore

APPROVAL_LEVELS = {1: "现场主管", 2: "项目经理", 3: "街区更新办公室(会同文保)"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ConstructionService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.plan = Plan()
        for event in store.all_events():
            self.plan.apply(event)

    # ================= 内部基元 =================

    def _exec(self, command_id: str | None, fn: Callable[[], dict]) -> dict:
        """幂等执行：重复 command_id 返回首次回执（跨重启逐字一致）。"""
        if command_id:
            existing = self.store.receipt_for(command_id)
            if existing is not None:
                return {"idempotent_replay": True, **existing}
        first_index = len(self.store.all_events())
        receipt = fn()
        if command_id:
            events = self.store.all_events()
            first = events[first_index] if first_index < len(events) else events[-1]
            receipt = {"command_id": command_id, "event_id": first.id, **receipt}
            # 回执本身落盘，重启重放后重复提交返回同一份内容
            self.store.append("COMMAND_RECEIPTED",
                              {"plan_id": Plan.PLAN_ID, "command_id": command_id,
                               "receipt": receipt}, command_id)
            self.store.remember_command(command_id, first, receipt)
        return receipt

    def _emit(self, event_type: str, payload: dict,
              command_id: str | None = None) -> Event:
        payload = {"plan_id": Plan.PLAN_ID, **payload}
        event = self.store.append(event_type, payload, command_id)
        self.plan.apply(event)
        return event

    # ================= 基础数据登记 =================

    def register_segment(self, segment_id: str, name: str, width_m: float,
                         zone_ids: tuple[str, ...] = (),
                         heritage_control: bool = False,
                         command_id: str | None = None) -> dict:
        def go() -> dict:
            if width_m <= 0:
                raise DomainError("区段宽度必须大于零")
            self._emit("SEGMENT_REGISTERED", {
                "segment_id": segment_id, "name": name, "width_m": width_m,
                "zone_ids": list(zone_ids), "heritage_control": heritage_control,
            }, command_id)
            return {"ok": True, "segment_id": segment_id}
        return self._exec(command_id, go)

    def register_crew(self, crew_id: str, name: str,
                      skills: tuple[str, ...] = (),
                      command_id: str | None = None) -> dict:
        def go() -> dict:
            self._emit("CREW_REGISTERED", {
                "crew_id": crew_id, "name": name, "skills": list(skills),
            }, command_id)
            return {"ok": True, "crew_id": crew_id}
        return self._exec(command_id, go)

    def register_protection(self, object_id: str, segment_id: str, kind: str,
                            name: str = "", buffer_m: float = 0.0,
                            required_level: int = 2,
                            command_id: str | None = None) -> dict:
        def go() -> dict:
            self.plan.require_segment(segment_id)
            if kind not in ("heritage", "utility", "unknown"):
                raise DomainError("保护对象类型须为 heritage/utility/unknown")
            self._emit("PROTECTION_REGISTERED", {
                "object_id": object_id, "segment_id": segment_id, "kind": kind,
                "name": name, "buffer_m": buffer_m,
                "required_level": required_level,
            }, command_id)
            return {"ok": True, "object_id": object_id}
        return self._exec(command_id, go)

    def add_access_commitment(self, segment_id: str, minimum_width_m: float,
                              always_open: bool = False,
                              command_id: str | None = None) -> dict:
        def go() -> dict:
            self.plan.require_segment(segment_id)
            if minimum_width_m <= 0:
                raise DomainError("通行宽度必须大于零")
            self._emit("ACCESS_COMMITMENT_MADE", {
                "segment_id": segment_id,
                "minimum_width_m": minimum_width_m,
                "always_open": always_open,
            }, command_id)
            return {"ok": True, "segment_id": segment_id}
        return self._exec(command_id, go)

    def declare_closure_window(self, segment_id: str, start_date: str,
                               end_date: str,
                               command_id: str | None = None) -> dict:
        def go() -> dict:
            self.plan.require_segment(segment_id)
            if parse_d(end_date) < parse_d(start_date):
                raise DomainError("封路窗口结束日早于开始日")
            self._emit("CLOSURE_WINDOW_DECLARED", {
                "segment_id": segment_id, "start_date": start_date,
                "end_date": end_date,
            }, command_id)
            return {"ok": True}
        return self._exec(command_id, go)

    def declare_open_window(self, segment_id: str, day: str,
                            work_allowed: bool = False, note: str = "",
                            command_id: str | None = None) -> dict:
        def go() -> dict:
            self.plan.require_segment(segment_id)
            parse_d(day)
            self._emit("OPEN_WINDOW_DECLARED", {
                "segment_id": segment_id, "date": day,
                "work_allowed": work_allowed, "note": note,
            }, command_id)
            return {"ok": True}
        return self._exec(command_id, go)

    def define_milestone(self, milestone_id: str, name: str, due_date: str,
                         task_ids: tuple[str, ...] = (),
                         command_id: str | None = None) -> dict:
        def go() -> dict:
            parse_d(due_date)
            missing = [t for t in task_ids if t not in self.plan.tasks]
            if missing:
                raise DomainError(f"里程碑引用任务不存在: {missing}")
            self._emit("MILESTONE_DEFINED", {
                "milestone_id": milestone_id, "name": name,
                "due_date": due_date, "task_ids": list(task_ids),
            }, command_id)
            return {"ok": True, "milestone_id": milestone_id}
        return self._exec(command_id, go)

    # ================= 任务登记 =================

    def register_task(self, task_id: str, segment_id: str,
                      depends_on: tuple[str, ...], duration_days: int,
                      crew_id: str | None = None, occupancy_width_m: float = 0.0,
                      requires_closure: bool = False,
                      earliest_start: str | None = None,
                      command_id: str | None = None) -> dict:
        def go() -> dict:
            self.plan.require_segment(segment_id)
            if duration_days < 1 or task_id in depends_on:
                raise DomainError("任务工期或依赖无效")
            self.plan.assert_dependencies_exist(tuple(depends_on))
            self.plan.assert_no_cycle(task_id, tuple(depends_on))
            if crew_id and crew_id not in self.plan.crews:
                raise DomainError(f"班组不存在: {crew_id}")
            self._emit("TASK_REGISTERED", {
                "task_id": task_id, "segment_id": segment_id,
                "depends_on": list(depends_on), "duration_days": duration_days,
                "crew_id": crew_id, "occupancy_width_m": occupancy_width_m,
                "requires_closure": requires_closure,
                "earliest_start": earliest_start,
            }, command_id)
            return {"ok": True, "task_id": task_id}
        return self._exec(command_id, go)

    # ================= 排程发布 =================

    def publish_schedule(self, start_date: str, command_id: str | None = None,
                         allow_conflicts: bool = False) -> dict:
        def go() -> dict:
            result = compute_schedule(self.plan, start_date)
            hard = [c for c in result.conflicts
                    if c["type"] in ("closure_window", "access_width",
                                     "missing_segment", "unschedulable",
                                     "dependency_blocked")]
            access = verify_access(self.plan, result.assignments)
            if ((hard or access["violations"]) and not allow_conflicts):
                raise DomainError({"conflicts": hard,
                                   "access_violations": access["violations"]})
            self._emit("SCHEDULE_PUBLISHED", {
                "start_date": start_date,
                "assignments": result.assignments,
                "conflicts": result.conflicts,
                "order": result.order,
                "published_at": _now(),
            }, command_id)
            access = verify_access(self.plan, result.assignments)
            return {"ok": True, "assignments": result.assignments,
                    "conflicts": result.conflicts,
                    "access": access}
        return self._exec(command_id, go)

    # ================= 现场：停工 / 复工 / 完工 / 验收 =================

    def suspend_task(self, task_id: str, reason: str,
                     discovered: dict | None = None,
                     command_id: str | None = None) -> dict:
        """现场发现未知管线/保护对象，停工上报。

        discovered 可携带新保护对象信息 {object_id, kind, name, buffer_m,
        required_level}，登记后该任务进入 SUSPENDED 并等待复工审批。
        """
        def go() -> dict:
            task = self.plan.require_task(task_id)
            if task.status == TaskStatus.SUSPENDED:
                raise DomainError(f"任务 {task_id} 已处于停工状态")
            self.plan.assert_task_mutable(task)
            detail: dict = {"prev_status": task.status.value}
            new_object_id = None
            if discovered:
                new_object_id = discovered.get(
                    "object_id") or _new_id("POBJ")
                self._emit("PROTECTION_REGISTERED", {
                    "object_id": new_object_id,
                    "segment_id": discovered.get("segment_id", task.segment_id),
                    "kind": discovered.get("kind", "unknown"),
                    "name": discovered.get("name", "现场未知物"),
                    "buffer_m": discovered.get("buffer_m", 1.0),
                    "required_level": discovered.get("required_level", 3),
                    "discovered_via_suspend": task_id,
                }, command_id)
                detail["protection_object_id"] = new_object_id
            self._emit("TASK_SUSPENDED", {
                "task_id": task_id, "reason": reason,
                "detail": {"prev_status": task.status.value,
                           **({"protection_object_id": new_object_id}
                              if new_object_id else {})},
                "suspended_at": _now(),
            }, command_id)
            return {"ok": True, "task_id": task_id, "status": "SUSPENDED",
                    "protection_object_id": new_object_id}
        return self._exec(command_id, go)

    def request_resume(self, task_id: str, safety_checks: dict[str, bool],
                       commander: str, command_id: str | None = None) -> dict:
        """申请安全复工：提交逐项安全条件（须全部满足）。"""
        def go() -> dict:
            task = self.plan.require_task(task_id)
            if task.status != TaskStatus.SUSPENDED:
                raise DomainError(f"任务 {task_id} 未停工，无需复工申请")
            if not safety_checks:
                raise DomainError("复工必须提交安全条件确认")
            unmet = [k for k, ok in safety_checks.items() if not ok]
            self._emit("RESUME_REQUESTED", {
                "task_id": task_id,
                "safety_checks": safety_checks,
                "unmet": unmet,
                "commander": commander,
                "requested_at": _now(),
            }, command_id)
            return {"ok": True, "task_id": task_id,
                    "awaiting_safety_approval": True, "unmet": unmet}
        return self._exec(command_id, go)

    def approve_resume(self, task_id: str, approver: str,
                       command_id: str | None = None) -> dict:
        """安全员核实全部条件满足、通行承诺不受影响后批准复工。"""
        def go() -> dict:
            task = self.plan.require_task(task_id)
            if task.status != TaskStatus.SUSPENDED:
                raise DomainError(f"任务 {task_id} 不在停工状态")
            detail = task.suspend_detail or {}
            req = detail.get("resume_request")
            if not req:
                raise DomainError("尚无复工申请，不能批准复工")
            if req.get("unmet"):
                raise DomainError(f"安全条件未全部满足: {req['unmet']}")
            # 复工后排程不得违反通行承诺
            candidate = compute_schedule(self.plan,
                                         self._schedule_anchor_date())
            access = verify_access(self.plan, candidate.assignments)
            bad = [v for v in access["violations"]
                   if any(tid == task_id for tid in v["task_ids"])]
            if bad:
                raise DomainError({"resume_blocked_access": bad})
            self._emit("TASK_RESUMED", {
                "task_id": task_id, "approver": approver,
                "resumed_at": _now(),
            }, command_id)
            return {"ok": True, "task_id": task_id, "status": task.status.value}
        return self._exec(command_id, go)

    def _schedule_anchor_date(self) -> str:
        # 以当前排期最早日或今天为推演起点
        if self.plan.schedule:
            starts = [a["start"] for a in self.plan.schedule.values()]
            return min(starts)
        return fmt_d(date.today())

    def record_progress(self, task_id: str, progress_days: int,
                        command_id: str | None = None) -> dict:
        """部分完工：登记已完成工日（不得超过总工期）。"""
        def go() -> dict:
            task = self.plan.require_task(task_id)
            if task.status in (TaskStatus.COMPLETED, TaskStatus.ACCEPTED):
                raise DomainError("任务已完工，进度锁定")
            if task.status == TaskStatus.SUSPENDED:
                raise DomainError("任务停工中，不能登记进度")
            if not 0 < progress_days <= task.duration_days:
                raise DomainError(
                    f"部分完工工日须在 1..{task.duration_days} 之间")
            if progress_days < task.progress_days:
                raise DomainError(
                    f"完工进度不可回退：已登记 {task.progress_days} 工日")
            # 登记满工日等同完工，前置必须先完工（在落事件前校验）
            if progress_days == task.duration_days:
                self.plan.assert_can_complete(task)
            self._emit("TASK_PROGRESSED", {
                "task_id": task_id, "progress_days": progress_days,
                "at": _now(),
            }, command_id)
            if progress_days == task.duration_days:
                self._emit("TASK_COMPLETED", {
                    "task_id": task_id, "at": _now(),
                }, command_id)
            return {"ok": True, "task_id": task_id,
                    "progress_days": progress_days}
        return self._exec(command_id, go)

    def complete_task(self, task_id: str, command_id: str | None = None) -> dict:
        def go() -> dict:
            task = self.plan.require_task(task_id)
            self.plan.assert_can_complete(task)
            self._emit("TASK_COMPLETED", {
                "task_id": task_id, "at": _now(),
            }, command_id)
            return {"ok": True, "task_id": task_id, "status": "COMPLETED"}
        return self._exec(command_id, go)

    def record_acceptance(self, task_id: str, inspector: str,
                          result: str, notes: str = "",
                          attachments: list[str] | None = None,
                          command_id: str | None = None) -> dict:
        """验收记录：一旦存在即锁定，任何后续改动（含重放）都不能覆盖。"""
        def go() -> dict:
            task = self.plan.require_task(task_id)
            self.plan.assert_can_accept(task)
            recorded_at = _now()
            self._emit("ACCEPTANCE_RECORDED", {
                "task_id": task_id, "inspector": inspector, "result": result,
                "notes": notes, "attachments": attachments or [],
                "recorded_at": recorded_at,
                "acceptance_id": _new_id("ACC"),
            }, command_id)
            return {"ok": True, "task_id": task_id, "status": "ACCEPTED"}
        return self._exec(command_id, go)

    # ================= 变更控制 =================

    def submit_change(self, kind: str, payload: dict, summary: str,
                      requested_by: str,
                      command_id: str | None = None) -> dict:
        """提交设计变更/计划调整，系统重新评估受影响任务与所需审批级别。"""
        def go() -> dict:
            if kind not in ("task_update", "closure_extension",
                            "scope_change", "other"):
                raise DomainError("未知变更类型")
            evaluation = self._evaluate_change(kind, payload)
            change_id = _new_id("CHG")
            self._emit("CHANGE_SUBMITTED", {
                "change_id": change_id, "kind": kind, "payload": payload,
                "summary": summary, "requested_by": requested_by,
                "required_level": evaluation["required_level"],
                "affected_tasks": evaluation["affected_tasks"],
                "conflicts": evaluation["conflicts"],
                "access_violations": evaluation["access_violations"],
                "milestone_impact": evaluation["milestone_impact"],
                "submitted_at": _now(),
            }, command_id)
            return {"ok": True, "change_id": change_id,
                    "required_level": evaluation["required_level"],
                    "required_role": APPROVAL_LEVELS[evaluation["required_level"]],
                    **{k: v for k, v in evaluation.items()
                       if k != "required_level"}}
        return self._exec(command_id, go)

    def _evaluate_change(self, kind: str, payload: dict) -> dict:
        plan = self.plan
        affected: set[str] = set()
        required_level = 1
        conflicts: list[dict] = []

        target = payload.get("task_id")
        candidate = compute_schedule(plan, self._schedule_anchor_date())

        if kind == "task_update" and target:
            task = plan.require_task(target)
            plan.assert_task_mutable(task)
            affected = _downstream(plan, target)
            # 同班组、同区段/空间的任务也可能被推移
            anchor = self._schedule_anchor_date()
            overrides = _override_for(payload, anchor)
            candidate = compute_schedule(
                plan, anchor, overrides={target: overrides},
                lock_existing=False)
            for tid, a in candidate.assignments.items():
                b = self.plan.schedule.get(tid)
                if b and (a["start"] != b["start"] or a["end"] != b["end"]):
                    affected.add(tid)
            seg = plan.segments[task.segment_id]
            if seg.get("heritage_control"):
                required_level = max(required_level, 3)
            # 邻近保护对象缓冲
            for obj in plan.protections.values():
                if obj["segment_id"] != task.segment_id:
                    continue
                if obj.get("buffer_m", 0) > 0 or obj["kind"] in (
                        "heritage", "unknown"):
                    required_level = max(required_level,
                                         int(obj["required_level"]))
            if payload.get("requires_closure") or task.requires_closure:
                required_level = max(required_level, 3)
        elif kind == "closure_extension":
            required_level = 3
            for c in plan.closures:
                if c["segment_id"] == payload["segment_id"]:
                    affected |= {t.task_id for t in plan.tasks.values()
                                 if t.segment_id == payload["segment_id"]}
        elif kind == "scope_change":
            required_level = 2
            if payload.get("segment_ids"):
                for sid in payload["segment_ids"]:
                    affected |= {t.task_id for t in plan.tasks.values()
                                 if t.segment_id == sid}

        # 静态冲突 + 通行承诺 + 里程碑
        conflicts.extend(c for c in candidate.conflicts)
        access = verify_access(plan, candidate.assignments)
        milestone_impact = [
            {"milestone_id": m["milestone_id"], "breached": m["breached"],
             "forecast_end": m["forecast_end"], "slack_days": m["slack_days"]}
            for m in (_ms_impact(plan, candidate.assignments))
            if m["breached"]
        ]
        if access["violations"]:
            required_level = max(required_level, 3)
        if milestone_impact:
            required_level = max(required_level, 2)
        return {
            "required_level": required_level,
            "affected_tasks": sorted(affected),
            "conflicts": conflicts,
            "access_violations": access["violations"],
            "milestone_impact": milestone_impact,
            "candidate_assignments": candidate.assignments,
        }

    def decide_change(self, change_id: str, approved: bool, approver: str,
                      level: int, rationale: str = "",
                      command_id: str | None = None) -> dict:
        def go() -> dict:
            cr = self._require_change(change_id)
            if cr.status != ChangeStatus.PENDING_APPROVAL:
                raise DomainError(f"变更 {change_id} 已{cr.status.value}")
            if level < cr.required_level:
                raise DomainError(
                    f"审批级别不足：需要 {cr.required_level}"
                    f"({APPROVAL_LEVELS[cr.required_level]})，提交为 {level}")
            if approved:
                # 先干跑：应用后若违反通行/封路约束，在批准落盘前拒绝，
                # 避免出现"已批准无法实施"的脏状态
                self._dry_run_change(cr)
            event_type = "CHANGE_APPROVED" if approved else "CHANGE_REJECTED"
            self._emit(event_type, {
                "change_id": change_id, "approver": approver, "level": level,
                "rationale": rationale, "decided_at": _now(),
            }, command_id)
            if approved:
                self._apply_change(cr, command_id)
            return {"ok": True, "change_id": change_id,
                    "status": "APPROVED" if approved else "REJECTED"}
        return self._exec(command_id, go)

    def _dry_run_change(self, cr: ChangeRequest) -> None:
        """在重放构建的临时计划上应用变更并校验，不触碰真实聚合。"""
        trial = Plan()
        for event in self.store.all_events():
            trial.apply(event)
        if cr.kind == "task_update":
            p = cr.payload
            updates = {k: v for k, v in p.items()
                       if k in ("segment_id", "duration_days", "crew_id",
                                "occupancy_width_m", "requires_closure",
                                "earliest_start")}
            if "depends_on" in p:
                updates["depends_on"] = list(p["depends_on"])
            trial.tasks[p["task_id"]].__dict__.update(
                {k: v for k, v in updates.items()
                 if k != "depends_on"})
            if "depends_on" in updates:
                trial.tasks[p["task_id"]].depends_on = tuple(
                    updates["depends_on"])
        elif cr.kind == "closure_extension":
            trial.closures.append({
                "segment_id": cr.payload["segment_id"],
                "start_date": cr.payload["start_date"],
                "end_date": cr.payload["end_date"]})
        anchor = min((a["start"] for a in trial.schedule.values()),
                     default=fmt_d(date.today()))
        result = compute_schedule(trial, anchor, lock_existing=False)
        access = verify_access(trial, result.assignments)
        problems: dict = {}
        if access["violations"]:
            problems["change_blocked_access"] = access["violations"]
        hard = [c for c in result.conflicts
                if c["type"] in ("closure_window", "unschedulable",
                                 "dependency_blocked", "access_width",
                                 "missing_segment")]
        if hard:
            problems["change_blocked_schedule"] = hard
        if problems:
            raise DomainError(problems)

    def _apply_change(self, cr: ChangeRequest, command_id: str | None) -> None:
        if cr.kind == "task_update":
            p = cr.payload
            task = self.plan.require_task(p["task_id"])
            self.plan.assert_task_mutable(task)
            updates = {k: v for k, v in p.items()
                       if k in ("segment_id", "duration_days", "crew_id",
                                "occupancy_width_m", "requires_closure",
                                "earliest_start")}
            new_deps = p.get("depends_on")
            if new_deps is not None:
                self.plan.assert_dependencies_exist(tuple(new_deps))
                self.plan.assert_no_cycle(p["task_id"], tuple(new_deps))
                updates["depends_on"] = list(new_deps)
            self._emit("TASK_UPDATED", {"task_id": p["task_id"], **updates},
                       command_id)
        elif cr.kind == "closure_extension":
            self._emit("CLOSURE_WINDOW_DECLARED", {
                "segment_id": cr.payload["segment_id"],
                "start_date": cr.payload["start_date"],
                "end_date": cr.payload["end_date"],
                "from_change": cr.change_id,
            }, command_id)
        # 重排并发布（变更后的任务不再锁定旧发布日期）
        result = compute_schedule(self.plan, self._schedule_anchor_date(),
                                  lock_existing=False)
        access = verify_access(self.plan, result.assignments)
        if access["violations"]:
            raise DomainError(
                {"change_blocked_access": access["violations"]})
        hard = [c for c in result.conflicts if c["type"] == "closure_window"]
        if hard:
            raise DomainError({"change_blocked_closure": hard})
        self._emit("SCHEDULE_PUBLISHED", {
            "start_date": self._schedule_anchor_date(),
            "assignments": result.assignments,
            "conflicts": result.conflicts, "order": result.order,
            "from_change": cr.change_id, "published_at": _now(),
        }, command_id)
        self._emit("CHANGE_APPLIED", {"change_id": cr.change_id,
                                      "applied_at": _now()}, command_id)

    def _require_change(self, change_id: str) -> ChangeRequest:
        if change_id not in self.plan.changes:
            raise DomainError(f"变更单不存在: {change_id}")
        return self.plan.changes[change_id]

    # ================= 基线与日计划 =================

    def take_baseline(self, label: str, command_id: str | None = None) -> dict:
        def go() -> dict:
            if not self.plan.schedule:
                raise DomainError("尚无已发布排期，不能建立里程碑基线")
            baseline_id = _new_id("BASE")
            self._emit("BASELINE_TAKEN", {
                "baseline_id": baseline_id, "label": label,
                "schedule": dict(self.plan.schedule),
                "milestones": list(self.plan.milestones.values()),
                "taken_at": _now(),
            }, command_id)
            return {"ok": True, "baseline_id": baseline_id}
        return self._exec(command_id, go)

    def issue_daily_plan(self, day: str, commander: str,
                         command_id: str | None = None) -> dict:
        """日计划签发：以最新发布排期生成当日作业令并校核通行承诺。"""
        def go() -> dict:
            parse_d(day)
            if not self.plan.schedule:
                raise DomainError("尚无已发布排期，不能签发日计划")
            if day in self.plan.daily_plans:
                raise DomainError(f"{day} 日计划已签发，如需调整请走变更流程")
            detail = build_daily_plan(self.plan, day)
            if not detail["access_satisfied"]:
                raise DomainError(
                    {"daily_plan_access_violation": detail["access_violations"]})
            self._emit("DAILY_PLAN_ISSUED", {
                "date": day, "commander": commander, "plan": detail,
                "issued_at": _now(),
            }, command_id)
            return {"ok": True, "date": day, "daily_plan": detail}
        return self._exec(command_id, go)

    # ================= 查询 =================

    def trace_delay(self, task_id: str, start_date: str | None = None,
                    delay_days: int | None = None,
                    duration_override: int | None = None) -> dict:
        return trace_delay(self.plan, start_date or self._schedule_anchor_date(),
                           task_id, delay_days=delay_days,
                           duration_override=duration_override)

    def verify_access(self, through_date: str | None = None) -> dict:
        return verify_access(self.plan, self.plan.schedule, through_date)

    def conflicts(self, start_date: str | None = None) -> list[dict]:
        result = compute_schedule(self.plan,
                                  start_date or self._schedule_anchor_date())
        return result.conflicts

    def state_snapshot(self) -> dict:
        p = self.plan
        return {
            "segments": p.segments, "crews": p.crews,
            "protections": p.protections, "commitments": p.commitments,
            "closure_windows": p.closures, "open_windows": p.open_windows,
            "milestones": p.milestones,
            "tasks": {tid: _task_dict(t) for tid, t in p.tasks.items()},
            "schedule": p.schedule,
            "changes": {cid: {"change_id": c.change_id, "kind": c.kind,
                              "status": c.status.value,
                              "required_level": c.required_level,
                              "affected_tasks": list(c.affected_tasks),
                              "summary": c.payload.get("summary", ""),
                              "decided_by": c.decided_by}
                       for cid, c in p.changes.items()},
            "pending_approvals": [cid for cid, c in p.changes.items()
                                  if c.status == ChangeStatus.PENDING_APPROVAL],
            "suspended_tasks": [tid for tid, t in p.tasks.items()
                                if t.status == TaskStatus.SUSPENDED],
            "baselines": [{"baseline_id": b["baseline_id"], "label": b["label"],
                           "taken_at": b["taken_at"]} for b in p.baselines],
            "daily_plans": list(p.daily_plans.keys()),
            "acceptances": p.acceptances,
            "event_count": len(self.store.all_events()),
        }


# ================= 辅助 =================

def _task_dict(t: TaskState) -> dict:
    return {"task_id": t.task_id, "segment_id": t.segment_id,
            "depends_on": list(t.depends_on), "duration_days": t.duration_days,
            "crew_id": t.crew_id, "occupancy_width_m": t.occupancy_width_m,
            "requires_closure": t.requires_closure,
            "earliest_start": t.earliest_start, "status": t.status.value,
            "version": t.version, "progress_days": t.progress_days,
            "suspend_reason": t.suspend_reason,
            "suspend_detail": t.suspend_detail}


def _downstream(plan: Plan, task_id: str) -> set[str]:
    dependents: dict[str, list[str]] = {}
    for t in plan.tasks.values():
        for d in t.depends_on:
            dependents.setdefault(d, []).append(t.task_id)
    out, stack = set(), list(dependents.get(task_id, ()))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(dependents.get(cur, ()))
    return out


def _override_for(payload: dict, anchor_date: str) -> dict:
    ov: dict[str, Any] = {}
    if "duration_days" in payload:
        ov["duration_days"] = payload["duration_days"]
    if "new_start" in payload:
        ov["start_no_earlier_than"] = payload["new_start"]
    elif "delay_days" in payload:
        new_earliest = parse_d(anchor_date) + timedelta(days=int(payload["delay_days"]))
        ov["start_no_earlier_than"] = fmt_d(new_earliest)
    return ov


def _ms_impact(plan: Plan, assignments: dict[str, dict]) -> list[dict]:
    from .scheduling import _milestone_impact
    return _milestone_impact(plan, assignments)

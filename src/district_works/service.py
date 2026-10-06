"""协同后端业务服务：状态机、变更评估分级、日计划签发、基线与幂等回执。"""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone

from . import schedule as sched_mod
from .contracts import AccessCommitment, WorkTask
from .models import (
    AcceptanceRecord,
    AcceptanceScope,
    ApprovalLevel,
    Baseline,
    ChangeKind,
    ChangeProposal,
    ClosureWindow,
    Crew,
    DailyDocket,
    DiscoveryKind,
    Incident,
    IncidentStatus,
    Milestone,
    ProposalStatus,
    ProtectedObject,
    Segment,
    TaskStatus,
)
from .repository import Repository


class ServiceError(Exception):
    """业务规则冲突（4xx 语义）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ProjectService:
    def __init__(self, repo: Repository, project_start: str | None = None, clock=_now):
        self.repo = repo
        self.clock = clock
        self.lock = threading.RLock()
        state = repo.load()
        self.segments: dict[str, Segment] = state["segments"]
        self.crews: dict[str, Crew] = state["crews"]
        self.objects: list[ProtectedObject] = state["objects"]
        self.windows: list[ClosureWindow] = state["windows"]
        self.milestones: list[Milestone] = state["milestones"]
        self.access: dict[str, AccessCommitment] = state["access"]
        self.tasks: dict[str, WorkTask] = state["tasks"]
        self.task_state: dict[str, dict] = state["task_state"]
        for tid in list(self.task_state):
            done = repo.get_meta(f"completed_on:{tid}")
            if done:
                self.task_state[tid]["completed_on"] = done
        self.incidents: dict[str, Incident] = {i.incident_id: i for i in state["incidents"]}
        self.proposals: dict[str, ChangeProposal] = {p.proposal_id: p for p in state["proposals"]}
        self.acceptances: list[AcceptanceRecord] = state["acceptances"]
        self.baselines: list[Baseline] = state["baselines"]
        self.dockets: list[DailyDocket] = state["dockets"]
        ps = repo.get_meta("project_start")
        self.project_start = project_start or ps or ""
        if self.project_start and not ps:
            repo.set_meta("project_start", self.project_start)
        self.as_of = repo.get_meta("as_of") or self.project_start or None

    def set_as_of(self, day: str) -> dict:
        """推进计划日历日；已完工任务之后的排程不会回到该日之前。"""
        if self.project_start and day < self.project_start:
            raise ServiceError("计划日历日不能早于项目开工日")
        self.as_of = day
        self.repo.set_meta("as_of", day)
        return {"as_of": day}

    # =====================================================================
    # 基础数据维护
    # =====================================================================
    def add_segment(self, segment_id: str, name: str, roadway_width_m: float) -> dict:
        if roadway_width_m <= 0:
            raise ServiceError("道路宽度必须大于零")
        seg = Segment(segment_id, name, roadway_width_m)
        self.segments[segment_id] = seg
        self.repo.upsert_segment(seg)
        return {"segment_id": segment_id}

    def add_crew(self, crew_id: str, name: str, trade: str = "") -> dict:
        crew = Crew(crew_id, name, trade)
        self.crews[crew_id] = crew
        self.repo.upsert_crew(crew)
        return {"crew_id": crew_id}

    def add_protected_object(self, object_id: str, segment_id: str, name: str,
                             radius_m: float = 0.0) -> dict:
        obj = ProtectedObject(object_id, segment_id, name, radius_m)
        self.objects = [o for o in self.objects if o.object_id != object_id]
        self.objects.append(obj)
        self.repo.upsert_object(obj)
        return {"object_id": object_id}

    def add_window(self, window_id: str, segment_id: str, start_date: str,
                   end_date: str) -> dict:
        if segment_id not in self.segments:
            raise ServiceError(f"区段不存在: {segment_id}")
        if start_date > end_date:
            raise ServiceError("封路窗口起止日期无效")
        w = ClosureWindow(window_id, segment_id, start_date, end_date)
        self.windows = [x for x in self.windows if x.window_id != window_id]
        self.windows.append(w)
        self.repo.upsert_window(w)
        return {"window_id": window_id}

    def add_milestone(self, milestone_id: str, name: str, due_date: str,
                      task_id: str | None = None) -> dict:
        if task_id and task_id not in self.tasks:
            raise ServiceError(f"里程碑锚点任务不存在: {task_id}")
        m = Milestone(milestone_id, name, due_date, task_id)
        self.milestones = [x for x in self.milestones if x.milestone_id != milestone_id]
        self.milestones.append(m)
        self.repo.upsert_milestone(m)
        return {"milestone_id": milestone_id}

    def set_access_commitment(self, segment_id: str, minimum_width_m: float,
                              always_open: bool, commitment_id: str = "") -> dict:
        if segment_id not in self.segments:
            raise ServiceError(f"区段不存在: {segment_id}")
        a = AccessCommitment(segment_id, minimum_width_m, always_open, commitment_id)
        self.access[segment_id] = a
        self.repo.upsert_access(a)
        return {"segment_id": segment_id, "minimum_width_m": minimum_width_m,
                "always_open": always_open}

    def add_task(self, task: WorkTask) -> dict:
        self._validate_task(task)
        self.tasks[task.task_id] = task
        self.task_state[task.task_id] = {"status": TaskStatus.PLANNED.value,
                                         "remaining_days": task.duration_days}
        self.repo.upsert_task(task, TaskStatus.PLANNED.value, task.duration_days)
        return {"task_id": task.task_id}

    def _validate_task(self, task: WorkTask) -> None:
        if task.segment_id not in self.segments:
            raise ServiceError(f"任务 {task.task_id} 引用了不存在的区段 {task.segment_id}")
        if task.crew_id and task.crew_id not in self.crews:
            raise ServiceError(f"任务 {task.task_id} 引用了不存在的施工队 {task.crew_id}")
        for dep in task.depends_on:
            if dep not in self.tasks:
                raise ServiceError(f"任务 {task.task_id} 依赖不存在的任务 {dep}")
        # 环检测交给排程引擎统一抛出

    # =====================================================================
    # 排程快照
    # =====================================================================
    def _stopped_task_ids(self) -> set[str]:
        return {tid for tid, st in self.task_state.items()
                if st["status"] == TaskStatus.STOPPED.value}

    def _completed_task_ids(self) -> set[str]:
        return {tid for tid, st in self.task_state.items()
                if st["status"] == TaskStatus.COMPLETED.value}

    def _held_task_ids(self) -> set[str]:
        return set(p for p in self.proposals
                   if self.proposals[p].status == ProposalStatus.PENDING.value
                   for tid in [self.proposals[p].changes.get("task_id")] if tid)

    def build_plan(self, overrides: dict | None = None) -> sched_mod.Plan:
        overrides = overrides or {}
        tasks = dict(self.tasks)
        if overrides.get("tasks"):
            tasks.update(overrides["tasks"])
        remaining = {tid: st["remaining_days"] for tid, st in self.task_state.items()
                     if st.get("remaining_days") is not None}
        completion_dates = {tid: st["completed_on"] for tid, st in self.task_state.items()
                            if st.get("completed_on")}
        if overrides.get("remaining_days"):
            remaining.update(overrides["remaining_days"])
        start_floors = {}
        if overrides.get("start_floors"):
            start_floors.update(overrides["start_floors"])
        return sched_mod.Plan(
            tasks=tasks, segments=self.segments, crews=self.crews,
            protected_objects=self.objects, windows=self.windows,
            milestones=self.milestones, access=self.access,
            project_start=self.project_start,
            stopped=set(overrides.get("stopped", self._stopped_task_ids())),
            completed=set(overrides.get("completed", self._completed_task_ids())),
            completion_dates=completion_dates,
            as_of=overrides.get("as_of", self.as_of or ""),
            remaining_days=remaining,
            held_for_approval=set(overrides.get("held", self._held_task_ids())),
            start_floors=start_floors)

    def compute_schedule(self, overrides: dict | None = None) -> sched_mod.ScheduleResult:
        with self.lock:
            return sched_mod.schedule(self.build_plan(overrides))

    def schedule_view(self) -> dict:
        result = self.compute_schedule()
        return {
            "feasible": result.feasible,
            "project_start": self.project_start,
            "tasks": [
                {"task_id": tid,
                 "start_date": sch.start_date or None,
                 "end_date": sch.end_date or None,
                 "blocked": sch.blocked,
                 "blocked_by": sch.blocked_by,
                 "status": self.task_state.get(tid, {}).get("status")}
                for tid, sch in sorted(result.tasks.items())],
            "order": result.order,
            "conflicts": [self._conflict_dict(c) for c in result.conflicts],
        }

    @staticmethod
    def _conflict_dict(c) -> dict:
        return {"type": c.conflict_type.value, "message": c.message,
                "task_ids": list(c.task_ids), "segment_id": c.segment_id,
                "crew_id": c.crew_id, "on_date": c.on_date}

    def verify_access(self) -> dict:
        plan = self.build_plan()
        return sched_mod.verify_access_commitment(plan)

    # =====================================================================
    # 开工 / 部分完工
    # =====================================================================
    def start_task(self, task_id: str, receipt_id: str | None = None) -> dict:
        return self._idempotent(receipt_id, "start_task", task_id,
                                lambda: self._start_task(task_id))

    def _start_task(self, task_id: str) -> dict:
        self._require_task(task_id)
        st = self.task_state[task_id]["status"]
        if st == TaskStatus.COMPLETED.value:
            raise ServiceError(f"任务 {task_id} 已完工，不能开工")
        if st == TaskStatus.STOPPED.value:
            raise ServiceError(f"任务 {task_id} 停工中，须走安全复工流程")
        result = self.compute_schedule()
        sch = result.tasks[task_id]
        if sch.blocked:
            raise ServiceError(f"前置任务未完成或任务被阻断: {sch.blocked_by}")
        if any(c.conflict_type.name == "PENDING_APPROVAL" and task_id in c.task_ids
               for c in result.conflicts):
            raise ServiceError(f"任务 {task_id} 的变更方案待批")
        self.repo.update_task_status(task_id, TaskStatus.IN_PROGRESS.value)
        self.task_state[task_id]["status"] = TaskStatus.IN_PROGRESS.value
        return {"task_id": task_id, "status": TaskStatus.IN_PROGRESS.value}

    def record_acceptance(self, task_id: str, scope: str, quantity: float,
                          note: str = "", recorder: str = "",
                          receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "acceptance", task_id,
            lambda: self._record_acceptance(task_id, scope, quantity, note, recorder))

    def _record_acceptance(self, task_id: str, scope: str, quantity: float,
                           note: str, recorder: str) -> dict:
        self._require_task(task_id)
        scope_enum = AcceptanceScope(scope)
        if quantity <= 0:
            raise ServiceError("验收工程量必须大于零")
        st = self.task_state[task_id]["status"]
        if st == TaskStatus.PLANNED.value:
            raise ServiceError(f"任务 {task_id} 尚未开工，无法验收")
        if st == TaskStatus.STOPPED.value:
            raise ServiceError(f"任务 {task_id} 停工中，无法验收")

        remaining = self.task_state[task_id]["remaining_days"]
        record = AcceptanceRecord(_new_id("ACC"), task_id, scope_enum, quantity,
                                  note, self.clock(), recorder)
        self.acceptances.append(record)
        self.repo.append_acceptance(record)

        if scope_enum is AcceptanceScope.FULL:
            # 完工日取当前排程的完工日（已含 as-of 与复工地板）
            done_day = self.compute_schedule().tasks[task_id].end_date or \
                self.as_of or date.today().isoformat()
            self.task_state[task_id]["status"] = TaskStatus.COMPLETED.value
            self.task_state[task_id]["remaining_days"] = 0
            self.task_state[task_id]["completed_on"] = done_day
            self.repo.update_task_status(task_id, TaskStatus.COMPLETED.value, 0)
            self.repo.set_meta(f"completed_on:{task_id}", done_day)
        else:
            # 部分完工：quantity 表示本次验收核减的作业天数，剩余至少保留 1 天
            new_remaining = max(1, remaining - max(0, round(quantity)))
            if st != TaskStatus.IN_PROGRESS.value:
                self.task_state[task_id]["status"] = TaskStatus.IN_PROGRESS.value
            self.task_state[task_id]["remaining_days"] = new_remaining
            self.repo.update_task_status(
                task_id, self.task_state[task_id]["status"], new_remaining)
        return {"record_id": record.record_id, "task_id": task_id,
                "scope": scope_enum.value,
                "status": self.task_state[task_id]["status"],
                "remaining_days": self.task_state[task_id]["remaining_days"]}

    def list_acceptances(self, task_id: str | None = None) -> list[dict]:
        records = [r for r in self.acceptances if not task_id or r.task_id == task_id]
        return [{"record_id": r.record_id, "task_id": r.task_id,
                 "scope": r.scope.value, "quantity": r.quantity, "note": r.note,
                 "recorded_at": r.recorded_at, "recorder": r.recorder}
                for r in records]

    # =====================================================================
    # 现场停工上报（未知管线 / 保护对象）
    # =====================================================================
    def report_discovery(self, task_id: str, kind: str, note: str = "",
                         receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "discovery", task_id,
            lambda: self._report_discovery(task_id, kind, note))

    def _report_discovery(self, task_id: str, kind: str, note: str) -> dict:
        self._require_task(task_id)
        kind_enum = DiscoveryKind(kind)
        st = self.task_state[task_id]["status"]
        if st == TaskStatus.COMPLETED.value:
            raise ServiceError(f"任务 {task_id} 已完工，不能停工")
        open_incident = next((i for i in self.incidents.values()
                              if i.task_id == task_id and
                              i.status is not IncidentStatus.CLEARED), None)
        if open_incident:
            raise ServiceError(f"任务 {task_id} 已有未闭环停工事件 {open_incident.incident_id}")

        incident = Incident(_new_id("INC"), task_id, kind_enum, note,
                            IncidentStatus.OPEN, self.clock())
        self.incidents[incident.incident_id] = incident
        self.repo.upsert_incident(incident)
        self.task_state[task_id]["status"] = TaskStatus.STOPPED.value
        self.repo.update_task_status(task_id, TaskStatus.STOPPED.value)

        result = self.compute_schedule()
        downstream = sorted(t for t in result.blocked if t != task_id)
        return {"incident_id": incident.incident_id, "task_id": task_id,
                "status": IncidentStatus.OPEN.value,
                "downstream_blocked": downstream}

    # =====================================================================
    # 变更方案：评估受影响任务 + 分级审批
    # =====================================================================
    def propose_change(self, kind: str, reason: str, changes: dict,
                       receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "proposal", changes.get("task_id", ""),
            lambda: self._propose_change(kind, reason, changes))

    def _propose_change(self, kind: str, reason: str, changes: dict) -> dict:
        kind_enum = ChangeKind(kind)
        if kind_enum is ChangeKind.TASK_UPDATE:
            task_id = changes.get("task_id")
            self._require_task(task_id)
            if self.task_state[task_id]["status"] == TaskStatus.COMPLETED.value:
                raise ServiceError(f"任务 {task_id} 已完工并验收，不允许变更")
        elif kind_enum is ChangeKind.WINDOW_SHIFT:
            if changes.get("window_id") not in {w.window_id for w in self.windows}:
                raise ServiceError("封路窗口不存在")
        elif kind_enum is ChangeKind.RESUME_AFTER_DISCOVERY:
            incident_id = changes.get("incident_id")
            incident = self.incidents.get(incident_id)
            if incident is None:
                raise ServiceError(f"停工事件不存在: {incident_id}")
            if incident.status is IncidentStatus.CLEARED:
                raise ServiceError("停工事件已闭环")

        # 应用候选变更得到试探计划
        candidate, candidate_tasks = self._apply_candidate(kind_enum, changes)
        result = sched_mod.schedule(candidate)

        affected = self._affected_tasks(kind_enum, changes, candidate_tasks, result)
        required = self._required_level(kind_enum, changes, affected, candidate, result)

        proposal = ChangeProposal(
            _new_id("PRP"), kind_enum, reason, changes, affected, required,
            ProposalStatus.PENDING, self.clock(),
            impact=self._impact_summary(result, affected, candidate))
        self.proposals[proposal.proposal_id] = proposal
        self.repo.upsert_proposal(proposal)
        return {"proposal_id": proposal.proposal_id,
                "required_level": required.value,
                "required_level_label": required.label,
                "affected_tasks": affected,
                "impact": proposal.impact,
                "status": ProposalStatus.PENDING.value}

    def _apply_candidate(self, kind: ChangeKind, changes: dict):
        """构造应用变更后的试探 Plan（不落库），同时返回候选任务集合。"""
        candidate_tasks: dict[str, WorkTask] = {}
        overrides: dict = {}
        if kind is ChangeKind.TASK_UPDATE:
            tid = changes["task_id"]
            old = self.tasks[tid]
            candidate_tasks[tid] = WorkTask(
                tid,
                changes.get("segment_id", old.segment_id),
                tuple(changes.get("depends_on", old.depends_on)),
                changes.get("duration_days", old.duration_days),
                changes.get("name", old.name),
                changes.get("crew_id", old.crew_id),
                changes.get("occupies_width_m", old.occupies_width_m),
                changes.get("requires_closure", old.requires_closure),
                changes.get("heritage_sensitive", old.heritage_sensitive),
                changes.get("earliest_start", old.earliest_start),
                changes.get("latest_finish", old.latest_finish))
            overrides["tasks"] = candidate_tasks
            # 评估时该任务挂起，不占用资源；其它未决方案仍挂起
            held = self._held_task_ids() | {tid}
            overrides["held"] = held
        elif kind is ChangeKind.RESUME_AFTER_DISCOVERY:
            incident = self.incidents[changes["incident_id"]]
            tid = incident.task_id
            overrides["stopped"] = self._stopped_task_ids() - {tid}
            if changes.get("resume_date"):
                overrides["start_floors"] = {tid: changes["resume_date"]}
            extra = changes.get("extra_days", 0)
            overrides["remaining_days"] = {
                tid: max(1, self.task_state[tid]["remaining_days"] + extra)}
            # 复工候选按“已复工”评估，使窗口/资源/通行冲突显式暴露
            overrides["held"] = self._held_task_ids()
        elif kind is ChangeKind.WINDOW_SHIFT:
            candidate_windows = []
            for w in self.windows:
                if w.window_id == changes["window_id"]:
                    candidate_windows.append(ClosureWindow(
                        w.window_id, w.segment_id,
                        changes.get("start_date", w.start_date),
                        changes.get("end_date", w.end_date)))
                else:
                    candidate_windows.append(w)
            plan = self.build_plan()
            plan.windows = candidate_windows
            return plan, candidate_tasks
        plan = self.build_plan(overrides)
        return plan, candidate_tasks

    def _affected_tasks(self, kind, changes, candidate_tasks, result) -> list[str]:
        """对比基线/当前排程，重新评估受影响任务（开工日变化、阻断、冲突）。"""
        base = self.compute_schedule()
        affected: set[str] = set()
        if kind is ChangeKind.TASK_UPDATE:
            affected.add(changes["task_id"])
        elif kind is ChangeKind.RESUME_AFTER_DISCOVERY:
            affected.add(self.incidents[changes["incident_id"]].task_id)
        elif kind is ChangeKind.WINDOW_SHIFT:
            seg_id = next((w.segment_id for w in self.windows
                           if w.window_id == changes["window_id"]), None)
            if seg_id:
                affected.update(t.task_id for t in self.tasks.values()
                                if t.segment_id == seg_id)
        for tid, sch in result.tasks.items():
            base_sch = base.tasks.get(tid)
            if not base_sch or sch.blocked != base_sch.blocked:
                affected.add(tid)
                continue
            if not sch.blocked and (sch.start_date != base_sch.start_date or
                                    sch.end_date != base_sch.end_date):
                affected.add(tid)
        for c in result.conflicts:
            if c.conflict_type.name in ("RESOURCE", "SPACE", "ACCESS",
                                        "OPEN_HOURS", "MILESTONE"):
                affected.update(c.task_ids)
        return sorted(t for t in affected if t)

    def _required_level(self, kind, changes, affected, candidate, result) -> ApprovalLevel:
        # 只依据变更直接作用的作业判定文保升级，下游传播不抬高级别
        direct_segments: set[str] = set()
        heritage = False
        if kind is ChangeKind.RESUME_AFTER_DISCOVERY:
            incident = self.incidents[changes["incident_id"]]
            heritage = incident.kind is DiscoveryKind.PROTECTED_OBJECT
            direct_segments.add(self.tasks[incident.task_id].segment_id)
        elif kind is ChangeKind.TASK_UPDATE:
            new_task = candidate.tasks[changes["task_id"]]
            heritage = bool(new_task.heritage_sensitive)
            direct_segments.add(new_task.segment_id)
        elif kind is ChangeKind.WINDOW_SHIFT:
            wid = changes["window_id"]
            direct_segments.update(w.segment_id for w in self.windows
                                   if w.window_id == wid)
        for obj in self.objects:
            if obj.segment_id in direct_segments:
                heritage = True
                break
        if heritage:
            return ApprovalLevel.HERITAGE
        if any(c.conflict_type is sched_mod.ConflictType.MILESTONE for c in result.conflicts) \
                or kind in (ChangeKind.WINDOW_SHIFT, ChangeKind.RESUME_AFTER_DISCOVERY):
            return ApprovalLevel.OFFICE
        return ApprovalLevel.SITE

    def _impact_summary(self, result, affected: list[str], candidate_plan) -> dict:
        return {
            "feasible": result.feasible,
            "conflicts": [self._conflict_dict(c) for c in result.conflicts
                          if c.conflict_type is not sched_mod.ConflictType.PENDING_APPROVAL],
            "affected_task_count": len(affected),
            "blocked_after": sorted(result.blocked),
            "access": sched_mod.verify_access_commitment(candidate_plan, result),
        }

    def decide_proposal(self, proposal_id: str, decision: str, approver: str,
                        level: str, note: str = "", receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "proposal_decision", proposal_id,
            lambda: self._decide_proposal(proposal_id, decision, approver, level, note))

    def _decide_proposal(self, proposal_id: str, decision: str, approver: str,
                         level: str, note: str) -> dict:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise ServiceError(f"变更方案不存在: {proposal_id}")
        if proposal.status is not ProposalStatus.PENDING:
            raise ServiceError(f"方案已 {proposal.status.value}，不能重复审批")
        decision_enum = ProposalStatus(decision)
        if decision_enum is ProposalStatus.PENDING:
            raise ServiceError("审批结论必须是 APPROVED 或 REJECTED")
        approver_level = ApprovalLevel.of(level)
        if approver_level.rank < proposal.required_level.rank:
            raise ServiceError(
                f"权限不足：该方案要求 {proposal_required_label(proposal)} 级别审批，"
                f"当前为 {approver_level.label}")

        proposal.status = decision_enum
        proposal.decided_at = self.clock()
        proposal.decided_by = approver
        proposal.decision_note = note
        self.repo.upsert_proposal(proposal)

        outcome = {"proposal_id": proposal_id, "decision": decision_enum.value,
                   "decided_by": approver}
        if decision_enum is ProposalStatus.APPROVED:
            outcome.update(self._apply_approved(proposal))
        return outcome

    def _apply_approved(self, proposal: ChangeProposal) -> dict:
        changes = proposal.changes
        if proposal.kind is ChangeKind.TASK_UPDATE:
            tid = changes["task_id"]
            old = self.tasks[tid]
            new_task = WorkTask(
                tid, changes.get("segment_id", old.segment_id),
                tuple(changes.get("depends_on", old.depends_on)),
                changes.get("duration_days", old.duration_days),
                changes.get("name", old.name), changes.get("crew_id", old.crew_id),
                changes.get("occupies_width_m", old.occupies_width_m),
                changes.get("requires_closure", old.requires_closure),
                changes.get("heritage_sensitive", old.heritage_sensitive),
                changes.get("earliest_start", old.earliest_start),
                changes.get("latest_finish", old.latest_finish))
            self._validate_task(new_task)
            self.tasks[tid] = new_task
            if self.task_state[tid]["status"] == TaskStatus.PLANNED.value:
                # 未开工：剩余工期跟随新总工期
                self.task_state[tid]["remaining_days"] = new_task.duration_days
                self.repo.upsert_task(new_task, TaskStatus.PLANNED.value,
                                      new_task.duration_days)
            else:
                # 已在进行：保留现场剩余工期
                self.repo.upsert_task(new_task, self.task_state[tid]["status"],
                                      self.task_state[tid]["remaining_days"],
                                      keep_remaining=True)
            return {"applied": "TASK_UPDATE", "task_id": tid}

        if proposal.kind is ChangeKind.WINDOW_SHIFT:
            wid = changes["window_id"]
            self.windows = [ClosureWindow(
                w.window_id, w.segment_id,
                changes.get("start_date", w.start_date),
                changes.get("end_date", w.end_date)) if w.window_id == wid else w
                for w in self.windows]
            for w in self.windows:
                if w.window_id == wid:
                    self.repo.upsert_window(w)
            return {"applied": "WINDOW_SHIFT", "window_id": wid}

        if proposal.kind is ChangeKind.RESUME_AFTER_DISCOVERY:
            incident = self.incidents[changes["incident_id"]]
            tid = incident.task_id
            extra = changes.get("extra_days", 0)
            if extra:
                new_remaining = max(1, self.task_state[tid]["remaining_days"] + extra)
                self.task_state[tid]["remaining_days"] = new_remaining
            incident.status = IncidentStatus.APPROVED_FOR_RESUME
            incident.proposal_id = proposal.proposal_id
            self.repo.upsert_incident(incident)
            self.repo.update_task_status(
                tid, TaskStatus.STOPPED.value, self.task_state[tid]["remaining_days"])
            # 复工地板日保留，供安全复工与排程使用
            if changes.get("resume_date"):
                self.repo.set_meta(f"resume_floor:{tid}", changes["resume_date"])
            return {"applied": "RESUME_AFTER_DISCOVERY", "incident_id": incident.incident_id,
                    "task_id": tid, "next_step": "现场确认安全后调用 safe_resume"}

        return {"applied": proposal.kind.value}

    # =====================================================================
    # 安全复工
    # =====================================================================
    def safe_resume(self, task_id: str, safety_checks: list[str],
                    resume_date: str | None = None, receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "safe_resume", task_id,
            lambda: self._safe_resume(task_id, safety_checks, resume_date))

    def _safe_resume(self, task_id: str, safety_checks: list[str],
                     resume_date: str | None) -> dict:
        self._require_task(task_id)
        incident = next((i for i in self.incidents.values()
                         if i.task_id == task_id and
                         i.status is not IncidentStatus.CLEARED), None)
        if incident is None:
            raise ServiceError(f"任务 {task_id} 没有未闭环的停工事件")
        if incident.status is IncidentStatus.OPEN:
            raise ServiceError("变更方案尚未获批，不能安全复工")
        if not safety_checks:
            raise ServiceError("复工必须提供现场安全核查项")
        # 复工前重新评估：方案批准后计划不应产生新的硬冲突（待审批项除外）
        floor = resume_date or self.repo.get_meta(f"resume_floor:{task_id}")
        overrides = {"stopped": self._stopped_task_ids() - {task_id},
                     "held": self._held_task_ids()}
        if floor:
            overrides["start_floors"] = {task_id: floor}
        result = sched_mod.schedule(self.build_plan(overrides))
        # 物理性硬冲突阻断复工；里程碑偏差属基线管理范畴，由基线对比跟踪
        hard = [c for c in result.conflicts
                if c.conflict_type not in (sched_mod.ConflictType.PENDING_APPROVAL,
                                           sched_mod.ConflictType.MILESTONE)]
        milestone_drift = [c.message for c in result.conflicts
                           if c.conflict_type is sched_mod.ConflictType.MILESTONE]
        if hard:
            raise ServiceError("复工评估发现未解决冲突: " +
                               "; ".join(c.message for c in hard))

        incident.status = IncidentStatus.CLEARED
        incident.cleared_at = self.clock()
        self.incidents[incident.incident_id] = incident
        self.repo.upsert_incident(incident)
        self.task_state[task_id]["status"] = TaskStatus.IN_PROGRESS.value
        self.repo.update_task_status(
            task_id, TaskStatus.IN_PROGRESS.value, self.task_state[task_id]["remaining_days"])
        return {"task_id": task_id, "incident_id": incident.incident_id,
                "status": TaskStatus.IN_PROGRESS.value,
                "resume_date": floor, "safety_checks": safety_checks}

    # =====================================================================
    # 里程碑基线
    # =====================================================================
    def create_baseline(self, name: str, receipt_id: str | None = None) -> dict:
        return self._idempotent(receipt_id, "baseline", name,
                                lambda: self._create_baseline(name))

    def _create_baseline(self, name: str) -> dict:
        result = self.compute_schedule()
        schedule_map = {tid: [sch.start_date, sch.end_date, sch.blocked]
                        for tid, sch in result.tasks.items()}
        milestone_values = {}
        for ms in self.milestones:
            if ms.task_id and not result.tasks[ms.task_id].blocked:
                milestone_values[ms.milestone_id] = result.tasks[ms.task_id].end_date
        baseline = Baseline(_new_id("BL"), name, self.clock(), schedule_map,
                            [{"milestone_id": m.milestone_id, "name": m.name,
                              "due_date": m.due_date, "task_id": m.task_id}
                             for m in self.milestones], milestone_values)
        self.baselines.append(baseline)
        self.repo.append_baseline(baseline)
        return {"baseline_id": baseline.baseline_id, "name": name,
                "milestone_values": milestone_values}

    def compare_baseline(self, baseline_id: str) -> dict:
        baseline = next((b for b in self.baselines if b.baseline_id == baseline_id), None)
        if baseline is None:
            raise ServiceError(f"基线不存在: {baseline_id}")
        result = self.compute_schedule()
        drift = []
        for tid, (start, end, blocked) in baseline.schedule.items():
            sch = result.tasks.get(tid)
            if sch is None:
                drift.append({"task_id": tid, "removed": True})
                continue
            end_shift = sched_mod.timeutil.days_between(end, sch.end_date) \
                if end and sch.end_date else None
            if blocked != sch.blocked or (end_shift and end_shift != 0):
                drift.append({"task_id": tid, "baseline_end": end,
                              "current_end": sch.end_date or None,
                              "end_shift_days": end_shift,
                              "was_blocked": blocked, "now_blocked": sch.blocked})
        ms_report = []
        for ms in self.milestones:
            projected = result.tasks[ms.task_id].end_date \
                if ms.task_id and ms.task_id in result.tasks and \
                not result.tasks[ms.task_id].blocked else None
            base_value = baseline.milestone_values.get(ms.milestone_id)
            ms_report.append({"milestone_id": ms.milestone_id, "name": ms.name,
                              "due_date": ms.due_date, "baseline_projected": base_value,
                              "current_projected": projected,
                              "on_time": projected is not None and projected <= ms.due_date})
        return {"baseline_id": baseline_id, "task_drift": drift,
                "milestones": ms_report}

    # =====================================================================
    # 日计划签发
    # =====================================================================
    def issue_daily_docket(self, plan_date: str, issued_by: str = "调度室",
                           receipt_id: str | None = None) -> dict:
        return self._idempotent(
            receipt_id, "docket", plan_date,
            lambda: self._issue_daily_docket(plan_date, issued_by))

    def _issue_daily_docket(self, plan_date: str, issued_by: str) -> dict:
        result = self.compute_schedule()
        entries, blocked_entries = [], []
        for tid, sch in result.tasks.items():
            state = self.task_state[tid]["status"]
            if sch.blocked:
                blocked_entries.append({"task_id": tid, "state": state,
                                        "blocked_by": sch.blocked_by})
                continue
            if sch.start_date <= plan_date <= sch.end_date:
                if state in (TaskStatus.PLANNED.value, TaskStatus.IN_PROGRESS.value):
                    entries.append({"task_id": tid, "segment_id": self.tasks[tid].segment_id,
                                    "crew_id": self.tasks[tid].crew_id, "state": state,
                                    "requires_closure": self.tasks[tid].requires_closure})
        # 当日冲突使该日计划不可签发
        todays_conflicts = [self._conflict_dict(c) for c in result.conflicts
                            if c.on_date == plan_date or
                            c.conflict_type is sched_mod.ConflictType.PENDING_APPROVAL]
        docket = DailyDocket(
            f"DK-{plan_date}-{self.repo.next_seq('docket_seq'):03d}",
            plan_date, self.clock(), issued_by, entries, blocked_entries,
            len(self.dockets) + 1)
        self.dockets.append(docket)
        self.repo.append_docket(docket)
        return {"docket_id": docket.docket_id, "plan_date": plan_date,
                "entries": entries, "blocked": blocked_entries,
                "conflicts": todays_conflicts,
                "issuable": not todays_conflicts,
                "note": "存在冲突，日计划标记为不可签发" if todays_conflicts else ""}

    # =====================================================================
    # 延期传播追溯
    # =====================================================================
    def trace_delay(self, task_id: str, delay_days: int) -> dict:
        plan = self.build_plan()
        return sched_mod.trace_delay(plan, task_id, delay_days)

    # =====================================================================
    # 幂等回执
    # =====================================================================
    def _idempotent(self, receipt_id: str | None, kind: str, ref_id: str, fn):
        with self.lock:
            if receipt_id:
                existing = self.repo.get_receipt(receipt_id)
                if existing is not None:
                    if existing["kind"] != kind or existing["ref_id"] != ref_id:
                        raise ServiceError(
                            f"回执 {receipt_id} 已用于其它操作，拒绝重复使用")
                    existing["result"]["idempotent_replay"] = True
                    return existing["result"]
            result = fn()
            if receipt_id:
                self.repo.append_receipt(receipt_id, kind, ref_id, self.clock(), result)
                result["receipt_id"] = receipt_id
            return result

    def _require_task(self, task_id: str) -> WorkTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise ServiceError(f"任务不存在: {task_id}")
        return task

    def status_view(self) -> dict:
        result = self.compute_schedule()
        return {
            "tasks": {tid: {"status": st["status"],
                            "remaining_days": st["remaining_days"]}
                      for tid, st in self.task_state.items()},
            "open_incidents": [
                {"incident_id": i.incident_id, "task_id": i.task_id,
                 "kind": i.kind.value, "status": i.status.value, "note": i.note}
                for i in self.incidents.values() if i.status is not IncidentStatus.CLEARED],
            "pending_proposals": [
                {"proposal_id": p.proposal_id, "kind": p.kind.value,
                 "required_level": p.required_level.value,
                 "affected_tasks": p.affected_tasks}
                for p in self.proposals.values() if p.status is ProposalStatus.PENDING],
            "blocked_in_schedule": sorted(result.blocked),
        }


def proposal_required_label(proposal: ChangeProposal) -> str:
    return proposal.required_level.label

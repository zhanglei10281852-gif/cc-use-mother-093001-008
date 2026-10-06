"""SQLite 持久化。

验收记录、里程碑基线、幂等回执均为仅追加表：通过触发器拒绝 UPDATE/DELETE，
保证“已完成的验收记录不能被后续改动覆盖”。
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .contracts import AccessCommitment, WorkTask
from .models import (
    AcceptanceRecord,
    AcceptanceScope,
    Baseline,
    ChangeProposal,
    ChangeKind,
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
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
  segment_id TEXT PRIMARY KEY, name TEXT NOT NULL, roadway_width_m REAL NOT NULL);
CREATE TABLE IF NOT EXISTS crews (
  crew_id TEXT PRIMARY KEY, name TEXT NOT NULL, trade TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS protected_objects (
  object_id TEXT PRIMARY KEY, segment_id TEXT NOT NULL, name TEXT NOT NULL,
  radius_m REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS windows (
  window_id TEXT PRIMARY KEY, segment_id TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones (
  milestone_id TEXT PRIMARY KEY, name TEXT NOT NULL, due_date TEXT NOT NULL,
  task_id TEXT);
CREATE TABLE IF NOT EXISTS access_commitments (
  segment_id TEXT PRIMARY KEY, minimum_width_m REAL NOT NULL,
  always_open INTEGER NOT NULL, commitment_id TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, segment_id TEXT NOT NULL, depends_on TEXT NOT NULL,
  duration_days INTEGER NOT NULL, name TEXT DEFAULT '', crew_id TEXT,
  occupies_width_m REAL DEFAULT 0, requires_closure INTEGER DEFAULT 0,
  heritage_sensitive INTEGER DEFAULT 0, earliest_start TEXT, latest_finish TEXT,
  status TEXT NOT NULL DEFAULT 'PLANNED', remaining_days INTEGER);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
  note TEXT DEFAULT '', status TEXT NOT NULL, reported_at TEXT NOT NULL,
  proposal_id TEXT DEFAULT '', cleared_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, kind TEXT NOT NULL, reason TEXT DEFAULT '',
  changes TEXT NOT NULL, affected_tasks TEXT NOT NULL,
  required_level TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  decided_at TEXT DEFAULT '', decided_by TEXT DEFAULT '',
  decision_note TEXT DEFAULT '', impact TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS acceptances (
  record_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope TEXT NOT NULL,
  quantity REAL NOT NULL, note TEXT DEFAULT '', recorded_at TEXT NOT NULL,
  recorder TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS baselines (
  baseline_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
  schedule TEXT NOT NULL, milestones TEXT NOT NULL, milestone_values TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dockets (
  docket_id TEXT PRIMARY KEY, plan_date TEXT NOT NULL, issued_at TEXT NOT NULL,
  issued_by TEXT DEFAULT '', entries TEXT NOT NULL, blocked TEXT NOT NULL,
  sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  created_at TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TRIGGER IF NOT EXISTS trg_acceptances_no_update
BEFORE UPDATE ON acceptances BEGIN
  SELECT RAISE(ABORT, '验收记录仅追加，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_acceptances_no_delete
BEFORE DELETE ON acceptances BEGIN
  SELECT RAISE(ABORT, '验收记录仅追加，禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS trg_baselines_no_update
BEFORE UPDATE ON baselines BEGIN
  SELECT RAISE(ABORT, '里程碑基线仅追加，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_baselines_no_delete
BEFORE DELETE ON baselines BEGIN
  SELECT RAISE(ABORT, '里程碑基线仅追加，禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS trg_receipts_no_update
BEFORE UPDATE ON receipts BEGIN
  SELECT RAISE(ABORT, '回执仅追加，禁止修改'); END;
"""


class Repository:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础数据 -------------------------------------------------------
    def upsert_segment(self, s: Segment) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO segments VALUES(?,?,?) ON CONFLICT(segment_id) "
                "DO UPDATE SET name=excluded.name, roadway_width_m=excluded.roadway_width_m",
                (s.segment_id, s.name, s.roadway_width_m))
            self.conn.commit()

    def upsert_crew(self, c: Crew) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO crews VALUES(?,?,?) ON CONFLICT(crew_id) DO UPDATE SET "
                "name=excluded.name, trade=excluded.trade",
                (c.crew_id, c.name, c.trade))
            self.conn.commit()

    def upsert_object(self, o: ProtectedObject) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO protected_objects VALUES(?,?,?,?) ON CONFLICT(object_id) "
                "DO UPDATE SET segment_id=excluded.segment_id, name=excluded.name, "
                "radius_m=excluded.radius_m", (o.object_id, o.segment_id, o.name, o.radius_m))
            self.conn.commit()

    def upsert_window(self, w: ClosureWindow) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO windows VALUES(?,?,?,?) ON CONFLICT(window_id) "
                "DO UPDATE SET segment_id=excluded.segment_id, start_date=excluded.start_date, "
                "end_date=excluded.end_date",
                (w.window_id, w.segment_id, w.start_date, w.end_date))
            self.conn.commit()

    def upsert_milestone(self, m: Milestone) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO milestones VALUES(?,?,?,?) ON CONFLICT(milestone_id) "
                "DO UPDATE SET name=excluded.name, due_date=excluded.due_date, "
                "task_id=excluded.task_id",
                (m.milestone_id, m.name, m.due_date, m.task_id))
            self.conn.commit()

    def upsert_access(self, a: AccessCommitment) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO access_commitments VALUES(?,?,?,?) ON CONFLICT(segment_id) "
                "DO UPDATE SET minimum_width_m=excluded.minimum_width_m, "
                "always_open=excluded.always_open, commitment_id=excluded.commitment_id",
                (a.segment_id, a.minimum_width_m, int(a.always_open), a.commitment_id))
            self.conn.commit()

    # ---- 任务 -----------------------------------------------------------
    def upsert_task(self, t: WorkTask, status: str, remaining_days: int | None,
                    keep_remaining: bool = False) -> None:
        remaining_sql = ("tasks.remaining_days" if keep_remaining
                         else "COALESCE(excluded.remaining_days, tasks.remaining_days)")
        with self._lock:
            self.conn.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET segment_id=excluded.segment_id, "
                "depends_on=excluded.depends_on, duration_days=excluded.duration_days, "
                "name=excluded.name, crew_id=excluded.crew_id, "
                "occupies_width_m=excluded.occupies_width_m, "
                "requires_closure=excluded.requires_closure, "
                "heritage_sensitive=excluded.heritage_sensitive, "
                "earliest_start=excluded.earliest_start, latest_finish=excluded.latest_finish, "
                f"remaining_days={remaining_sql}",
                (t.task_id, t.segment_id, json.dumps(list(t.depends_on)),
                 t.duration_days, t.name, t.crew_id, t.occupies_width_m,
                 int(t.requires_closure), int(t.heritage_sensitive),
                 t.earliest_start, t.latest_finish, status,
                 remaining_days if remaining_days is not None else t.duration_days))
            self.conn.commit()

    def update_task_status(self, task_id: str, status: str,
                           remaining_days: int | None = None) -> None:
        with self._lock:
            if remaining_days is None:
                self.conn.execute("UPDATE tasks SET status=? WHERE task_id=?",
                                  (status, task_id))
            else:
                self.conn.execute("UPDATE tasks SET status=?, remaining_days=? WHERE task_id=?",
                                  (status, remaining_days, task_id))
            self.conn.commit()

    # ---- 停工事件 / 变更方案 --------------------------------------------
    def upsert_incident(self, i: Incident) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) "
                "DO UPDATE SET status=excluded.status, proposal_id=excluded.proposal_id, "
                "cleared_at=excluded.cleared_at",
                (i.incident_id, i.task_id, i.kind.value, i.note, i.status.value,
                 i.reported_at, i.proposal_id, i.cleared_at))
            self.conn.commit()

    def upsert_proposal(self, p: ChangeProposal) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET status=excluded.status, "
                "decided_at=excluded.decided_at, decided_by=excluded.decided_by, "
                "decision_note=excluded.decision_note, impact=excluded.impact, "
                "affected_tasks=excluded.affected_tasks, changes=excluded.changes",
                (p.proposal_id, p.kind.value, p.reason, json.dumps(p.changes, ensure_ascii=False),
                 json.dumps(p.affected_tasks, ensure_ascii=False), p.required_level.value,
                 p.status.value, p.created_at, p.decided_at, p.decided_by,
                 p.decision_note, json.dumps(p.impact, ensure_ascii=False)))
            self.conn.commit()

    # ---- 仅追加：验收 / 基线 / 日计划 / 回执 ----------------------------
    def append_acceptance(self, r: AcceptanceRecord) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO acceptances VALUES(?,?,?,?,?,?,?)",
                              (r.record_id, r.task_id, r.scope.value, r.quantity,
                               r.note, r.recorded_at, r.recorder))
            self.conn.commit()

    def append_baseline(self, b: Baseline) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?)",
                              (b.baseline_id, b.name, b.created_at,
                               json.dumps(b.schedule, ensure_ascii=False),
                               json.dumps(b.milestones, ensure_ascii=False),
                               json.dumps(b.milestone_values, ensure_ascii=False)))
            self.conn.commit()

    def append_docket(self, d: DailyDocket) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO dockets VALUES(?,?,?,?,?,?,?)",
                              (d.docket_id, d.plan_date, d.issued_at, d.issued_by,
                               json.dumps(d.entries, ensure_ascii=False),
                               json.dumps(d.blocked, ensure_ascii=False), d.sequence))
            self.conn.commit()

    def append_receipt(self, receipt_id: str, kind: str, ref_id: str,
                       created_at: str, result: dict) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",
                              (receipt_id, kind, ref_id, created_at,
                               json.dumps(result, ensure_ascii=False)))
            self.conn.commit()

    def get_receipt(self, receipt_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM receipts WHERE receipt_id=?",
                                    (receipt_id,)).fetchone()
        if not row:
            return None
        return {"receipt_id": row["receipt_id"], "kind": row["kind"],
                "ref_id": row["ref_id"], "created_at": row["created_at"],
                "result": json.loads(row["result"])}

    # ---- meta 计数 ------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self.conn.commit()

    def next_seq(self, key: str) -> int:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            n = (int(row["value"]) + 1) if row else 1
            self.conn.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(n)))
            self.conn.commit()
            return n

    # ---- 全量装载（重启恢复）-------------------------------------------
    def load(self) -> dict:
        with self._lock:
            def rows(sql):
                return self.conn.execute(sql).fetchall()

            segments = {r["segment_id"]: Segment(r["segment_id"], r["name"], r["roadway_width_m"])
                        for r in rows("SELECT * FROM segments")}
            crews = {r["crew_id"]: Crew(r["crew_id"], r["name"], r["trade"])
                     for r in rows("SELECT * FROM crews")}
            objects = [ProtectedObject(r["object_id"], r["segment_id"], r["name"], r["radius_m"])
                       for r in rows("SELECT * FROM protected_objects")]
            windows = [ClosureWindow(r["window_id"], r["segment_id"], r["start_date"], r["end_date"])
                       for r in rows("SELECT * FROM windows")]
            milestones = [Milestone(r["milestone_id"], r["name"], r["due_date"], r["task_id"])
                          for r in rows("SELECT * FROM milestones")]
            access = {r["segment_id"]: AccessCommitment(
                r["segment_id"], r["minimum_width_m"], bool(r["always_open"]),
                r["commitment_id"]) for r in rows("SELECT * FROM access_commitments")}

            tasks, task_state = {}, {}
            for r in rows("SELECT * FROM tasks"):
                t = WorkTask(
                    r["task_id"], r["segment_id"], tuple(json.loads(r["depends_on"])),
                    r["duration_days"], r["name"], r["crew_id"], r["occupies_width_m"],
                    bool(r["requires_closure"]), bool(r["heritage_sensitive"]),
                    r["earliest_start"], r["latest_finish"])
                tasks[t.task_id] = t
                task_state[t.task_id] = {"status": r["status"],
                                         "remaining_days": r["remaining_days"]}

            incidents = [Incident(
                r["incident_id"], r["task_id"], DiscoveryKind(r["kind"]), r["note"],
                IncidentStatus(r["status"]), r["reported_at"], r["proposal_id"],
                r["cleared_at"]) for r in rows("SELECT * FROM incidents")]

            proposals = [ChangeProposal(
                r["proposal_id"], ChangeKind(r["kind"]), r["reason"],
                json.loads(r["changes"]), json.loads(r["affected_tasks"]),
                _approval(r["required_level"]),
                ProposalStatus(r["status"]), r["created_at"], r["decided_at"],
                r["decided_by"], r["decision_note"], json.loads(r["impact"] or "{}"))
                for r in rows("SELECT * FROM proposals")]

            acceptances = [AcceptanceRecord(
                r["record_id"], r["task_id"], AcceptanceScope(r["scope"]), r["quantity"],
                r["note"], r["recorded_at"], r["recorder"])
                for r in rows("SELECT * FROM acceptances")]

            baselines = [Baseline(
                r["baseline_id"], r["name"], r["created_at"],
                json.loads(r["schedule"]), json.loads(r["milestones"]),
                json.loads(r["milestone_values"])) for r in rows("SELECT * FROM baselines")]

            dockets = [DailyDocket(
                r["docket_id"], r["plan_date"], r["issued_at"], r["issued_by"],
                json.loads(r["entries"]), json.loads(r["blocked"]), r["sequence"])
                for r in rows("SELECT * FROM dockets")]

        return {"segments": segments, "crews": crews, "objects": objects,
                "windows": windows, "milestones": milestones, "access": access,
                "tasks": tasks, "task_state": task_state,
                "incidents": incidents, "proposals": proposals,
                "acceptances": acceptances, "baselines": baselines, "dockets": dockets}

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def _approval(code: str):
    from .models import ApprovalLevel
    return ApprovalLevel.of(code)

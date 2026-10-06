import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.contracts import WorkTask
from district_works.repository import Repository
from district_works.service import ProjectService, ServiceError


def build_project(path=":memory:", start="2026-10-08"):
    svc = ProjectService(Repository(path), start)
    svc.add_segment("S1", "北段", 6.0)
    svc.add_segment("S2", "南段", 5.0)
    svc.add_crew("C1", "管网队")
    svc.add_crew("C2", "恢复队")
    svc.add_protected_object("P1", "S2", "明代排水沟", 3.0)
    svc.set_access_commitment("S1", 1.2, False)
    svc.set_access_commitment("S2", 1.5, True)
    svc.add_window("W1", "S1", "2026-10-08", "2026-10-12")
    svc.add_window("W2", "S2", "2026-10-08", "2026-10-30")
    svc.add_task(WorkTask("T1", "S1", (), 3, "主管", "C1",
                          occupies_width_m=3.0, requires_closure=True))
    svc.add_task(WorkTask("T2", "S2", ("T1",), 3, "南段管", "C1",
                          occupies_width_m=3.5, heritage_sensitive=True))
    svc.add_task(WorkTask("T3", "S1", ("T1",), 2, "恢复", "C2",
                          occupies_width_m=2.0))
    svc.add_milestone("M1", "贯通", "2026-10-16", "T2")
    return svc


class ScheduleBasicsTests(unittest.TestCase):
    def test_initial_plan_is_feasible(self):
        self.assertTrue(build_project().compute_schedule().feasible)

    def test_resource_conflict_surfaced(self):
        svc = build_project()
        svc.add_task(WorkTask("TX", "S2", (), 3, "冲突任务", "C1",
                              occupies_width_m=1.0))
        view = svc.schedule_view()
        self.assertFalse(view["feasible"])
        self.assertIn("RESOURCE", {c["type"] for c in view["conflicts"]})


class AcceptanceTests(unittest.TestCase):
    def test_partial_then_full_completion(self):
        svc = build_project()
        svc.start_task("T1")
        r1 = svc.record_acceptance("T1", "PARTIAL", 2)
        self.assertEqual(r1["remaining_days"], 1)
        self.assertEqual(r1["status"], "IN_PROGRESS")
        r2 = svc.record_acceptance("T1", "FULL", 1)
        self.assertEqual(r2["status"], "COMPLETED")

    def test_cannot_accept_unstarted_or_stopped(self):
        svc = build_project()
        with self.assertRaises(ServiceError):
            svc.record_acceptance("T1", "FULL", 1)
        svc.start_task("T1")
        svc.report_discovery("T1", "UNKNOWN_UTILITY", "x")
        with self.assertRaises(ServiceError):
            svc.record_acceptance("T1", "PARTIAL", 1)

    def test_acceptance_records_are_sql_immutable(self):
        svc = build_project()
        svc.start_task("T1")
        rec = svc.record_acceptance("T1", "PARTIAL", 1)
        with self.assertRaises(sqlite3.IntegrityError):
            svc.repo.conn.execute("UPDATE acceptances SET quantity=9 WHERE record_id=?",
                                  (rec["record_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            svc.repo.conn.execute("DELETE FROM acceptances WHERE record_id=?",
                                  (rec["record_id"],))

    def test_completed_task_rejects_change_proposal(self):
        svc = build_project()
        svc.start_task("T1")
        svc.record_acceptance("T1", "FULL", 3)
        with self.assertRaises(ServiceError):
            svc.propose_change("TASK_UPDATE", "x", {"task_id": "T1", "duration_days": 9})


class IncidentResumeFlowTests(unittest.TestCase):
    def _stop(self, svc):
        svc.start_task("T1")
        return svc.report_discovery("T1", "UNKNOWN_UTILITY", "发现未知燃气管")

    def test_stop_blocks_downstream(self):
        svc = build_project()
        self._stop(svc)
        blocked = svc.compute_schedule().blocked
        self.assertEqual(blocked, {"T1", "T2", "T3"})

    def test_duplicate_open_incident_rejected(self):
        svc = build_project()
        self._stop(svc)
        with self.assertRaises(ServiceError):
            svc.report_discovery("T1", "UNKNOWN_UTILITY", "再次上报")

    def test_protected_discovery_requires_heritage(self):
        svc = build_project()
        svc.start_task("T2")
        inc = svc.report_discovery("T2", "PROTECTED_OBJECT", "新发现柱础")
        prop = svc.propose_change("RESUME_AFTER_DISCOVERY", "避让",
                                  {"incident_id": inc["incident_id"]})
        self.assertEqual(prop["required_level"], "HERITAGE")

    def test_full_resume_flow(self):
        svc = build_project()
        inc = self._stop(svc)
        prop = svc.propose_change(
            "RESUME_AFTER_DISCOVERY", "探明后改线",
            {"incident_id": inc["incident_id"], "extra_days": 6,
             "resume_date": "2026-10-13"})
        # 低级别不能批
        with self.assertRaises(ServiceError):
            svc.decide_proposal(prop["proposal_id"], "APPROVED", "工长", "SITE")
        # 未知管线 → OFFICE 级即可
        self.assertEqual(prop["required_level"], "OFFICE")
        svc.decide_proposal(prop["proposal_id"], "APPROVED", "更新办", "OFFICE")
        # 未做安全核查不能复工
        with self.assertRaises(ServiceError):
            svc.safe_resume("T1", [])
        # 复工日期超出窗口 → 阻断（T1 需要封路）
        with self.assertRaises(ServiceError):
            svc.safe_resume("T1", ["已探明管线"], resume_date="2026-10-13")
        # 先延长窗口（OFFICE），再复工
        win = svc.propose_change("WINDOW_SHIFT", "配合复工",
                                 {"window_id": "W1", "end_date": "2026-10-22"})
        self.assertEqual(win["required_level"], "OFFICE")
        svc.decide_proposal(win["proposal_id"], "APPROVED", "更新办", "OFFICE")
        out = svc.safe_resume("T1", ["管线探测", "支护复核", "临时通道"],
                              resume_date="2026-10-13")
        self.assertEqual(out["status"], "IN_PROGRESS")
        self.assertEqual(svc.status_view()["open_incidents"], [])

    def test_rejected_proposal_allows_new_one_and_no_apply(self):
        svc = build_project()
        inc = self._stop(svc)
        prop = svc.propose_change(
            "RESUME_AFTER_DISCOVERY", "",
            {"incident_id": inc["incident_id"]})
        svc.decide_proposal(prop["proposal_id"], "REJECTED", "更新办", "OFFICE",
                            note="方案不可行")
        self.assertEqual(svc.task_state["T1"]["status"], "STOPPED")
        prop2 = svc.propose_change(
            "RESUME_AFTER_DISCOVERY", "修改方案",
            {"incident_id": inc["incident_id"]})
        self.assertEqual(prop2["status"], "PENDING")


class IdempotencyTests(unittest.TestCase):
    def test_same_receipt_replays_first_result(self):
        svc = build_project()
        a = svc.start_task("T1", receipt_id="R1")
        b = svc.start_task("T1", receipt_id="R1")
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertTrue(b["idempotent_replay"])

    def test_receipt_cross_operation_rejected(self):
        svc = build_project()
        svc.start_task("T1", receipt_id="R9")
        with self.assertRaises(ServiceError):
            svc.report_discovery("T1", "UNKNOWN_UTILITY", "x", receipt_id="R9")

    def test_distinct_receipts_execute_twice(self):
        svc = build_project()
        svc.start_task("T1", receipt_id="RA")
        svc.record_acceptance("T1", "PARTIAL", 1, receipt_id="RB")
        again = svc.record_acceptance("T1", "PARTIAL", 1, receipt_id="RC")
        self.assertEqual(again["remaining_days"], 1)  # 3-2=1


class BaselineDocketTests(unittest.TestCase):
    def test_baseline_drift_after_approved_change(self):
        svc = build_project()
        bl = svc.create_baseline("基线")
        before = svc.compare_baseline(bl["baseline_id"])
        self.assertTrue(all(m["on_time"] for m in before["milestones"]))

        prop = svc.propose_change("TASK_UPDATE", "加井位",
                                  {"task_id": "T1", "duration_days": 8})
        svc.decide_proposal(prop["proposal_id"], "APPROVED", "更新办", "OFFICE")
        after = svc.compare_baseline(bl["baseline_id"])
        drifted = {d["task_id"]: d["end_shift_days"] for d in after["task_drift"]}
        self.assertIn("T2", drifted)
        self.assertTrue(any(not m["on_time"] for m in after["milestones"]))

    def test_docket_blocks_when_conflict(self):
        svc = build_project()
        ok = svc.issue_daily_docket("2026-10-08")
        self.assertTrue(ok["issuable"])
        svc.add_task(WorkTask("TX", "S1", (), 1, "同日封路", "C2",
                              requires_closure=True))
        bad = svc.issue_daily_docket("2026-10-08")
        self.assertFalse(bad["issuable"])
        self.assertTrue(bad["conflicts"])


class RecoveryTests(unittest.TestCase):
    def test_restart_restores_stopped_and_pending(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            svc = build_project(f.name)
            svc.start_task("T1")
            inc = svc.report_discovery("T1", "UNKNOWN_UTILITY", "未知管线")
            svc.propose_change("RESUME_AFTER_DISCOVERY", "待批",
                               {"incident_id": inc["incident_id"]})
            svc2 = ProjectService(Repository(f.name))
            view = svc2.status_view()
            self.assertEqual(view["tasks"]["T1"]["status"], "STOPPED")
            self.assertEqual(len(view["pending_proposals"]), 1)
            self.assertEqual(view["open_incidents"][0]["status"], "OPEN")

    def test_restart_restores_completed_and_acceptances(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            svc = build_project(f.name)
            svc.start_task("T1")
            svc.record_acceptance("T1", "FULL", 3)
            svc2 = ProjectService(Repository(f.name))
            self.assertEqual(svc2.task_state["T1"]["status"], "COMPLETED")
            self.assertEqual(len(svc2.list_acceptances("T1")), 1)
            self.assertNotIn("T1", svc2.compute_schedule().blocked)


if __name__ == "__main__":
    unittest.main()

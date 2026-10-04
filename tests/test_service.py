import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.domain import DomainError, TaskStatus
from district_works.service import ConstructionService
from district_works.store import EventStore


def new_service(path: str) -> ConstructionService:
    svc = ConstructionService(EventStore(path))
    svc.register_segment("S1", "东街", 4.0, ("ZA",))
    svc.register_segment("S2", "古巷", 3.0, ("ZA",), heritage_control=True)
    svc.register_crew("C1", "甲班")
    svc.register_crew("C2", "乙班")
    svc.register_protection("P1", "S2", "heritage", "古宅墙基",
                            buffer_m=2.0, required_level=3)
    svc.add_access_commitment("S2", 1.5, True)
    svc.declare_closure_window("S1", "2026-11-02", "2026-11-20")
    svc.register_task("T1", "S1", (), 2, "C1", 1.0)
    svc.register_task("T2", "S2", ("T1",), 2, "C1", 1.0)
    svc.register_task("T3", "S1", ("T1",), 2, "C2", 1.0,
                      requires_closure=True, earliest_start="2026-11-02")
    svc.publish_schedule("2026-11-02")
    return svc


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.svc = new_service(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 幂等 ----------

    def test_duplicate_command_returns_same_receipt(self):
        r1 = self.svc.register_crew("CX", "重复班", command_id="cmd-1")
        r2 = self.svc.register_crew("CX", "重复班", command_id="cmd-1")
        self.assertNotIn("idempotent_replay", r1)
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r1["event_id"], r2["event_id"])
        self.assertEqual(len(self.svc.plan.crews), 3)  # 没有重复登记

    def test_idempotent_receipt_survives_restart(self):
        self.svc.suspend_task(
            "T2", "未知管线",
            discovered={"kind": "unknown", "name": "陶土管"},
            command_id="sus-1")
        svc2 = ConstructionService(EventStore(self.path))
        r = svc2.suspend_task("T2", "再次", command_id="sus-1")
        self.assertTrue(r["idempotent_replay"])
        self.assertEqual(r["status"], "SUSPENDED")
        self.assertIsNotNone(r.get("protection_object_id"))

    # ---------- 停工 / 复工 ----------

    def test_suspend_registers_unknown_object_and_blocks_completion(self):
        r = self.svc.suspend_task(
            "T2", "发现未知管线",
            discovered={"kind": "unknown", "name": "未知排水管"},
            command_id="sus-2")
        self.assertEqual(self.svc.plan.tasks["T2"].status,
                         TaskStatus.SUSPENDED)
        self.assertIn(r["protection_object_id"], self.svc.plan.protections)
        with self.assertRaises(DomainError):
            self.svc.complete_task("T2")

    def test_resume_requires_request_and_all_checks(self):
        self.svc.suspend_task("T2", "未知物", command_id="s3")
        with self.assertRaises(DomainError):
            self.svc.approve_resume("T2", "安全员")
        self.svc.request_resume(
            "T2", {"交底": True, "支护": False}, "班长")
        with self.assertRaises(DomainError):
            self.svc.approve_resume("T2", "安全员")
        self.svc.request_resume(
            "T2", {"交底": True, "支护": True}, "班长")
        r = self.svc.approve_resume("T2", "安全员")
        self.assertTrue(r["ok"])
        self.assertNotEqual(self.svc.plan.tasks["T2"].status,
                            TaskStatus.SUSPENDED)

    def test_suspended_state_recovers_after_restart(self):
        self.svc.suspend_task("T2", "未知物", command_id="s4")
        self.svc.request_resume("T2", {"交底": True}, "班长")
        svc2 = ConstructionService(EventStore(self.path))
        self.assertEqual(svc2.plan.tasks["T2"].status,
                         TaskStatus.SUSPENDED)
        self.assertIsNotNone(
            svc2.plan.tasks["T2"].suspend_detail.get("resume_request"))

    # ---------- 完工 / 验收 ----------

    def test_acceptance_is_immutable(self):
        self.svc.complete_task("T1", command_id="d1")
        self.svc.record_acceptance("T1", "监理", "合格", command_id="a1")
        with self.assertRaises(DomainError):
            self.svc.record_acceptance("T1", "监理", "不合格")
        with self.assertRaises(DomainError):
            self.svc.complete_task("T1")
        # 重启后仍然锁定
        svc2 = ConstructionService(EventStore(self.path))
        with self.assertRaises(DomainError):
            svc2.record_acceptance("T1", "监理", "不合格")

    def test_cannot_complete_before_dependencies(self):
        with self.assertRaises(DomainError):
            self.svc.complete_task("T2")

    def test_partial_completion_tracks_progress(self):
        self.svc.record_progress("T1", 1)
        self.assertEqual(self.svc.plan.tasks["T1"].progress_days, 1)
        self.assertEqual(self.svc.plan.tasks["T1"].status,
                         TaskStatus.IN_PROGRESS)
        with self.assertRaises(DomainError):
            self.svc.record_progress("T1", 99)

    # ---------- 变更控制 ----------

    def test_closure_task_change_requires_level_3(self):
        r = self.svc.submit_change(
            "task_update", {"task_id": "T3", "duration_days": 5},
            "工期延长", "设计方", command_id="chg-1")
        self.assertEqual(r["required_level"], 3)
        self.assertIn("T3", r["affected_tasks"])

    def test_underlevel_approval_rejected_then_approved_applies(self):
        cid = self.svc.submit_change(
            "task_update", {"task_id": "T3", "duration_days": 4},
            "x", "设计方")["change_id"]
        with self.assertRaises(DomainError):
            self.svc.decide_change(cid, True, "项目经理", 2)
        r = self.svc.decide_change(cid, True, "办公室", 3)
        self.assertEqual(r["status"], "APPROVED")
        self.assertEqual(self.svc.plan.tasks["T3"].duration_days, 4)
        self.assertEqual(self.svc.plan.tasks["T3"].version, 2)
        # 已决定的变更不能重复审批
        with self.assertRaises(DomainError):
            self.svc.decide_change(cid, False, "办公室", 3)

    def test_accepted_task_cannot_change(self):
        self.svc.complete_task("T1")
        self.svc.record_acceptance("T1", "监理", "合格")
        with self.assertRaises(DomainError):
            self.svc.submit_change(
                "task_update", {"task_id": "T1", "duration_days": 9},
                "x", "设计方")

    def test_rejected_change_leaves_plan_unchanged(self):
        before = self.svc.plan.tasks["T3"].duration_days
        cid = self.svc.submit_change(
            "task_update", {"task_id": "T3", "duration_days": 6},
            "x", "设计方")["change_id"]
        self.svc.decide_change(cid, False, "办公室", 3, rationale="不同意")
        self.assertEqual(self.svc.plan.tasks["T3"].duration_days, before)

    # ---------- 日计划 / 基线 ----------

    def test_daily_plan_idempotent_per_day_and_baseline(self):
        first_day = self.svc.plan.schedule["T1"]["start"]
        self.svc.issue_daily_plan(first_day, "调度")
        with self.assertRaises(DomainError):
            self.svc.issue_daily_plan(first_day, "调度")
        r = self.svc.take_baseline("基线A")
        self.assertTrue(r["baseline_id"])
        self.assertEqual(len(self.svc.plan.baselines), 1)

    # ---------- 通行承诺 ----------

    def test_publish_blocked_when_always_open_violated(self):
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "e.jsonl")
        svc = ConstructionService(EventStore(path))
        svc.register_segment("S9", "窄巷", 3.0)
        svc.add_access_commitment("S9", 1.5, True)
        svc.declare_closure_window("S9", "2026-11-02", "2026-11-10")
        svc.register_task("X1", "S9", (), 1, None, 0.0,
                          requires_closure=True)
        with self.assertRaises(DomainError) as ctx:
            svc.publish_schedule("2026-11-02")
        self.assertIn("access_violations", ctx.exception.args[0])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

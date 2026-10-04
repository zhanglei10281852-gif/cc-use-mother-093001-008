import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.domain import Plan
from district_works.scheduling import (compute_schedule, trace_delay,
                                       verify_access, build_daily_plan)
from district_works.events import Event


def build_plan() -> Plan:
    plan = Plan()

    def emit(t, p):
        e = Event(seq=build_plan.seq, type=t, payload={"plan_id": "P", **p},
                  id=f"E-{build_plan.seq}")
        build_plan.seq += 1
        plan.apply(e)

    emit("SEGMENT_REGISTERED", {"segment_id": "S1", "name": "东街", "width_m": 4.0,
                                "zone_ids": ["ZA"], "heritage_control": False})
    emit("SEGMENT_REGISTERED", {"segment_id": "S2", "name": "古巷", "width_m": 3.0,
                                "zone_ids": ["ZA"], "heritage_control": True})
    emit("CREW_REGISTERED", {"crew_id": "C1", "name": "甲班", "skills": []})
    emit("ACCESS_COMMITMENT_MADE", {"segment_id": "S2", "minimum_width_m": 1.5,
                                    "always_open": True})
    emit("CLOSURE_WINDOW_DECLARED", {"segment_id": "S1",
                                     "start_date": "2026-11-02",
                                     "end_date": "2026-11-20"})
    for d in ("2026-11-04", "2026-11-11"):
        emit("OPEN_WINDOW_DECLARED", {"segment_id": "S2", "date": d,
                                      "work_allowed": False, "note": "集市"})
    emit("TASK_REGISTERED", {"task_id": "T1", "segment_id": "S1",
                             "depends_on": [], "duration_days": 3,
                             "crew_id": "C1", "occupancy_width_m": 1.0,
                             "requires_closure": False})
    emit("TASK_REGISTERED", {"task_id": "T2", "segment_id": "S2",
                             "depends_on": ["T1"], "duration_days": 2,
                             "crew_id": "C1", "occupancy_width_m": 1.0,
                             "requires_closure": False})
    emit("TASK_REGISTERED", {"task_id": "T3", "segment_id": "S1",
                             "depends_on": ["T1"], "duration_days": 2,
                             "crew_id": None, "occupancy_width_m": 0.0,
                             "requires_closure": True})
    return plan


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        build_plan.seq = 0
        self.plan = build_plan()

    def test_respects_dependency_order(self):
        r = compute_schedule(self.plan, "2026-11-02")
        self.assertEqual(r.assignments["T1"]["start"], "2026-11-02")
        self.assertGreaterEqual(r.assignments["T2"]["start"], "2026-11-05")

    def test_crew_conflict_serializes_tasks(self):
        r = compute_schedule(self.plan, "2026-11-02")
        busy = {}
        for tid, a in r.assignments.items():
            if self.plan.tasks[tid].crew_id == "C1":
                for d in a["days"]:
                    self.assertNotIn(d, busy, f"班组同日重叠: {d}")
                    busy[d] = tid

    def test_open_window_dates_skipped(self):
        r = compute_schedule(self.plan, "2026-11-02")
        days = r.assignments["T2"]["days"]
        self.assertNotIn("2026-11-04", days)
        self.assertNotIn("2026-11-11", days)

    def test_closure_task_must_fit_window(self):
        r = compute_schedule(self.plan, "2026-11-02")
        for d in r.assignments["T3"]["days"]:
            self.assertTrue("2026-11-02" <= d <= "2026-11-20")
        # 无窗口时封路任务不可排
        build_plan.seq = 0
        p2 = build_plan()
        p2.closures.clear()
        r2 = compute_schedule(p2, "2026-11-02")
        self.assertTrue(any(c["type"] == "unschedulable"
                            and c["task_id"] == "T3" for c in r2.conflicts))

    def test_shared_space_serializes_segments(self):
        # S1/S2 同属 ZONE_A：同日两处都作业不允许
        r = compute_schedule(self.plan, "2026-11-02")
        occ: dict[str, str] = {}
        for tid, a in r.assignments.items():
            for d in a["days"]:
                self.assertNotIn(d, occ, f"同空间组同日冲突 {d}: {occ.get(d)}/{tid}")
                occ[d] = tid

    def test_access_width_static_conflict(self):
        p = Plan()
        e = Event(0, "SEGMENT_REGISTERED", {"segment_id": "S", "width_m": 3.0,
                                            "zone_ids": []}, "e0")
        p.apply(e)
        p.apply(Event(1, "ACCESS_COMMITMENT_MADE",
                      {"segment_id": "S", "minimum_width_m": 2.0,
                       "always_open": False}, "e1"))
        p.apply(Event(2, "TASK_REGISTERED",
                      {"task_id": "X", "segment_id": "S", "depends_on": [],
                       "duration_days": 1, "occupancy_width_m": 2.0}, "e2"))
        r = compute_schedule(p, "2026-11-02")
        self.assertTrue(any(c["type"] == "access_width" for c in r.conflicts))

    def test_trace_delay_propagates_downstream(self):
        r = trace_delay(self.plan, "2026-11-02", "T1", delay_days=3)
        root = next(c for c in r["propagation_chain"] if c["root"])
        self.assertEqual(root["task_id"], "T1")
        downstream = {c["task_id"]: c for c in r["propagation_chain"]}
        self.assertIn("T2", downstream)
        self.assertGreaterEqual(downstream["T2"]["shift_days"], 3)

    def test_unschedulable_root_cascades_and_is_flagged(self):
        # T3 封路窗口只在 11-02..11-05，强制 T1 延至窗口外后，T3 排不下
        r = trace_delay(self.plan, "2026-11-02", "T1", delay_days=30)
        flagged = {c["task_id"]: c for c in r["propagation_chain"]}
        self.assertIn("T3", flagged)
        self.assertTrue(flagged["T3"].get("unschedulable"))

    def test_verify_access_reports_always_open_violation(self):
        r = compute_schedule(self.plan, "2026-11-02")
        # 人为制造违规：T3(封路) 放到 S2
        bad = dict(r.assignments)
        bad["T3"] = {"start": "2026-11-05", "end": "2026-11-05",
                     "days": ["2026-11-05"], "segment_id": "S2",
                     "crew_id": None}
        self.plan.tasks["T3"].segment_id = "S2"
        report = verify_access(self.plan, bad)
        self.assertFalse(report["satisfied"])
        self.assertTrue(any(v["rule"] == "always_open"
                            for v in report["violations"]))

    def test_daily_plan_lists_only_that_day(self):
        r = compute_schedule(self.plan, "2026-11-02")
        day = build_daily_plan(self.plan, r.assignments["T1"]["days"][0],
                               r.assignments)
        self.assertIn("T1", [t["task_id"] for t in day["tasks"]])


if __name__ == "__main__":
    unittest.main()

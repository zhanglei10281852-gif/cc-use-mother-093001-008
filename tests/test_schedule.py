import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.contracts import AccessCommitment, WorkTask
from district_works.models import ClosureWindow, Crew, Segment
from district_works.schedule import (
    ConflictType,
    Plan,
    schedule,
    trace_delay,
    verify_access_commitment,
)


def base_plan(**kw):
    tasks = {
        "A": WorkTask("A", "S1", (), 2, crew_id="C1", occupies_width_m=3.0),
        "B": WorkTask("B", "S1", ("A",), 2, crew_id="C1", occupies_width_m=3.0),
        "C": WorkTask("C", "S2", (), 2, crew_id="C2", occupies_width_m=2.0),
    }
    plan = Plan(tasks=tasks,
                segments={"S1": Segment("S1", "街", 6.0),
                          "S2": Segment("S2", "巷", 4.0)},
                crews={"C1": Crew("C1", "甲"), "C2": Crew("C2", "乙")},
                project_start="2026-10-08")
    for k, v in kw.items():
        setattr(plan, k, v)
    return plan


class TopologyTests(unittest.TestCase):
    def test_dependency_orders_successors(self):
        r = schedule(base_plan())
        self.assertEqual(r.tasks["A"].end_date, "2026-10-09")
        self.assertEqual(r.tasks["B"].start_date, "2026-10-10")
        self.assertTrue(r.feasible)

    def test_dependency_cycle_raises(self):
        plan = base_plan()
        plan.tasks["A"] = WorkTask("A", "S1", ("B",), 2)
        with self.assertRaises(ValueError):
            schedule(plan)

    def test_missing_dependency_raises(self):
        plan = base_plan()
        plan.tasks["A"] = WorkTask("A", "S1", ("X",), 2)
        with self.assertRaises(ValueError):
            schedule(plan)

    def test_stopped_task_blocks_downstream(self):
        plan = base_plan(stopped={"A"})
        r = schedule(plan)
        self.assertIn("A", r.blocked)
        self.assertIn("B", r.blocked)
        self.assertNotIn("C", r.blocked)

    def test_completed_task_consumed_and_not_occupied(self):
        plan = base_plan(completed={"A"}, completion_dates={"A": "2026-10-09"})
        r = schedule(plan)
        self.assertEqual(r.tasks["B"].start_date, "2026-10-10")
        self.assertNotIn("A", r.blocked)


class ResourceConflictTests(unittest.TestCase):
    def test_same_crew_same_day_conflicts(self):
        plan = base_plan()
        # A 与 C 改为同队同日
        plan.tasks["C"] = WorkTask("C", "S2", (), 2, crew_id="C1", occupies_width_m=2.0)
        r = schedule(plan)
        self.assertTrue(r.conflicts_of(ConflictType.RESOURCE))

    def test_sequential_crew_assignment_is_fine(self):
        r = schedule(base_plan())
        self.assertFalse(r.conflicts_of(ConflictType.RESOURCE))


class SpaceAccessTests(unittest.TestCase):
    def test_space_overload_detected(self):
        plan = base_plan()
        plan.tasks["B"] = WorkTask("B", "S1", ("A",), 2, crew_id="C1",
                                   occupies_width_m=4.0)
        plan.tasks["D"] = WorkTask("D", "S1", ("A",), 2, crew_id="C2",
                                   occupies_width_m=3.0)
        r = schedule(plan)
        self.assertTrue(any(c.conflict_type is ConflictType.SPACE for c in r.conflicts))

    def test_access_width_breach_detected(self):
        plan = base_plan()
        plan.access = {"S1": AccessCommitment("S1", 3.5, False)}
        r = schedule(plan)
        breaches = r.conflicts_of(ConflictType.ACCESS)
        self.assertTrue(breaches)

    def test_always_open_forbids_closure(self):
        plan = base_plan()
        plan.tasks["A"] = WorkTask("A", "S1", (), 2, crew_id="C1",
                                   requires_closure=True)
        plan.access = {"S1": AccessCommitment("S1", 1.0, True)}
        plan.windows = [ClosureWindow("W1", "S1", "2026-10-08", "2026-10-20")]
        r = schedule(plan)
        self.assertTrue(any("始终开放" in c.message for c in r.conflicts_of(ConflictType.ACCESS)))

    def test_closure_inside_window_exempts_width_but_still_scheduled(self):
        plan = base_plan()
        plan.tasks["A"] = WorkTask("A", "S1", (), 2, crew_id="C1",
                                   requires_closure=True)
        plan.access = {"S1": AccessCommitment("S1", 1.0, False)}
        plan.windows = [ClosureWindow("W1", "S1", "2026-10-08", "2026-10-20")]
        r = schedule(plan)
        self.assertTrue(r.feasible)

    def test_closure_outside_window_is_open_hours_conflict(self):
        plan = base_plan()
        plan.tasks["A"] = WorkTask("A", "S1", (), 2, crew_id="C1",
                                   requires_closure=True)
        plan.windows = [ClosureWindow("W1", "S1", "2026-10-20", "2026-10-22")]
        r = schedule(plan)
        self.assertTrue(r.conflicts_of(ConflictType.OPEN_HOURS))

    def test_verify_access_report_structure(self):
        plan = base_plan()
        plan.access = {"S1": AccessCommitment("S1", 2.0, False)}
        report = verify_access_commitment(plan)
        self.assertIn("S1", report)
        self.assertTrue(report["S1"]["checked"])
        self.assertTrue(report["S1"]["satisfied"])


class DelayTraceTests(unittest.TestCase):
    def test_delay_propagates_to_successors(self):
        plan = base_plan()
        trace = trace_delay(plan, "A", 3)
        self.assertEqual(trace["origin_new_end"], "2026-10-12")
        shifted = {p["task_id"]: p["shift_days"] for p in trace["propagation"]}
        self.assertEqual(shifted.get("B"), 3)
        self.assertNotIn("C", shifted)  # 无依赖关系不受影响

    def test_delay_without_dependents_is_local(self):
        trace = trace_delay(base_plan(), "C", 2)
        self.assertEqual(trace["propagation"], [])

    def test_negative_delay_rejected(self):
        with self.assertRaises(ValueError):
            trace_delay(base_plan(), "A", -1)


if __name__ == "__main__":
    unittest.main()

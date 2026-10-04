import json, sys, tempfile, threading, unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.api import build_server


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_path = str(Path(self.tmp.name) / "events.jsonl")
        self.server = build_server(store_path, port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_flow_over_http(self):
        s, r = self._post("/segments", {"segment_id": "S1", "name": "东街",
                                        "width_m": 4.0})
        self.assertEqual(s, 201)
        self._post("/crews", {"crew_id": "C1", "name": "甲班"})
        self._post("/tasks", {"task_id": "T1", "segment_id": "S1",
                              "depends_on": [], "duration_days": 2,
                              "crew_id": "C1",
                              "command_id": "t1"})
        # 幂等
        s2, r2 = self._post("/tasks", {"task_id": "T1", "segment_id": "S1",
                                       "depends_on": [], "duration_days": 2,
                                       "crew_id": "C1",
                                       "command_id": "t1"})
        self.assertTrue(r2["idempotent_replay"])

        s3, r3 = self._post("/schedule", {"start_date": "2026-11-02"})
        self.assertEqual(s3, 201)
        self.assertEqual(r3["assignments"]["T1"]["start"], "2026-11-02")

        s4, r4 = self._get("/access")
        self.assertTrue(r4["satisfied"])

        s5, r5 = self._get("/delay-trace/T1?delay=3")
        self.assertEqual(r5["trigger_task"], "T1")
        self.assertTrue(r5["propagation_chain"][0]["root"])

    def test_task_action_routes(self):
        self._post("/segments", {"segment_id": "S1", "name": "东街",
                                 "width_m": 4.0})
        self._post("/crews", {"crew_id": "C1", "name": "甲班"})
        self._post("/tasks", {"task_id": "T1", "segment_id": "S1",
                              "depends_on": [], "duration_days": 2,
                              "crew_id": "C1"})
        s, r = self._post("/tasks/T1/suspend",
                          {"reason": "未知管线",
                           "discovered": {"kind": "unknown", "name": "排水管"}})
        self.assertEqual(s, 201)
        self.assertEqual(r["status"], "SUSPENDED")
        s2, r2 = self._post("/tasks/T1/resume-request",
                            {"safety_checks": {"交底": True},
                             "commander": "李班"})
        self.assertTrue(r2["awaiting_safety_approval"])
        s3, r3 = self._post("/tasks/T1/resume-approve",
                            {"approver": "王安全"})
        self.assertEqual(s3, 201)
        self.assertIn(r3["status"], ("SCHEDULED", "IN_PROGRESS", "PENDING"))

    def test_change_route_and_decision(self):
        self._post("/segments", {"segment_id": "S1", "name": "东街",
                                 "width_m": 4.0, "heritage_control": True})
        self._post("/tasks", {"task_id": "T1", "segment_id": "S1",
                              "depends_on": [], "duration_days": 2,
                              "requires_closure": True})
        s, r = self._post("/changes", {
            "kind": "task_update",
            "payload": {"task_id": "T1", "duration_days": 4},
            "summary": "延长", "requested_by": "设计"})
        self.assertEqual(s, 201)
        cid = r["change_id"]
        self.assertEqual(r["required_level"], 3)

        s2, r2 = self._get(f"/changes/{cid}")
        self.assertEqual(s2, 200)
        self.assertEqual(r2["status"], "PENDING_APPROVAL")

        code, _ = self._post(f"/changes/{cid}/decision",
                             {"approved": False, "approver": "办公室",
                              "level": 3})
        self.assertEqual(code, 201)

    def test_domain_conflict_returns_4xx(self):
        self._post("/segments", {"segment_id": "S1", "name": "东街",
                                 "width_m": 4.0})
        self._post("/tasks", {"task_id": "T1", "segment_id": "S1",
                              "depends_on": [], "duration_days": 1})
        s, r = self._post("/tasks", {"task_id": "T2", "segment_id": "S1",
                                     "depends_on": ["NOPE"],
                                     "duration_days": 1})
        self.assertEqual(s, 422)
        self.assertEqual(r["error"], "domain_error")


if __name__ == "__main__":
    unittest.main()

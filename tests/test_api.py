import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.api import build_server


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.server = build_server("127.0.0.1", 0, ":memory:", "2026-10-08")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.req("POST", "/segments", {"segment_id": "S1", "name": "北段",
                                       "roadway_width_m": 6.0})
        self.req("POST", "/crews", {"crew_id": "C1", "name": "管网队"})
        self.req("POST", "/access-commitments",
                 {"segment_id": "S1", "minimum_width_m": 1.2, "always_open": False})
        self.req("POST", "/windows", {"window_id": "W1", "segment_id": "S1",
                                      "start_date": "2026-10-08",
                                      "end_date": "2026-10-20"})
        self.req("POST", "/tasks", {"task_id": "T1", "segment_id": "S1",
                                    "depends_on": [], "duration_days": 3,
                                    "crew_id": "C1", "requires_closure": True})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json",
                                            **(headers or {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class HttpFlowTests(ApiTestBase):
    def test_health_and_schedule(self):
        self.assertEqual(self.req("GET", "/health")[0], 200)
        _, sched = self.req("GET", "/schedule")
        self.assertTrue(sched["feasible"])

    def test_start_accept_and_complete(self):
        self.assertEqual(self.req("POST", "/tasks/T1/start")[0], 200)
        code, acc = self.req("POST", "/tasks/T1/acceptances",
                             {"scope": "FULL", "quantity": 3})
        self.assertEqual(code, 201)
        self.assertEqual(acc["status"], "COMPLETED")
        _, accs = self.req("GET", "/tasks/T1/acceptances")
        self.assertEqual(len(accs), 1)

    def test_incident_approval_resume_over_http(self):
        self.req("POST", "/tasks/T1/start")
        _, inc = self.req("POST", "/incidents",
                          {"task_id": "T1", "kind": "UNKNOWN_UTILITY",
                           "note": "未知管线"})
        _, prop = self.req("POST", "/proposals",
                           {"kind": "RESUME_AFTER_DISCOVERY",
                            "changes": {"incident_id": inc["incident_id"],
                                        "resume_date": "2026-10-12"}})
        self.assertEqual(prop["required_level"], "OFFICE")
        code, err = self.req("POST", f"/proposals/{prop['proposal_id']}/decision",
                             {"decision": "APPROVED", "level": "SITE",
                              "approver": "工长"})
        self.assertEqual(code, 409)
        code, ok = self.req("POST", f"/proposals/{prop['proposal_id']}/decision",
                            {"decision": "APPROVED", "level": "OFFICE",
                             "approver": "更新办"})
        self.assertEqual(code, 200)
        code, resumed = self.req("POST", "/tasks/T1/safe-resume",
                                 {"safety_checks": ["管线探测"],
                                  "resume_date": "2026-10-12"})
        self.assertEqual(code, 200)
        self.assertEqual(resumed["status"], "IN_PROGRESS")

    def test_idempotency_key_header(self):
        hdr = {"Idempotency-Key": "DUP-1"}
        a = self.req("POST", "/tasks/T1/start", {}, hdr)
        b = self.req("POST", "/tasks/T1/start", {}, hdr)
        self.assertEqual(a[1]["task_id"], b[1]["task_id"])
        self.assertTrue(b[1]["idempotent_replay"])
        _, receipt = self.req("GET", "/receipts/DUP-1")
        self.assertEqual(receipt["kind"], "start_task")

    def test_docket_and_baseline_and_trace(self):
        _, bl = self.req("POST", "/baselines", {"name": "b"})
        self.assertEqual(self.req("GET", f"/baselines/{bl['baseline_id']}/comparison")[0], 200)
        _, dk = self.req("POST", "/dockets", {"plan_date": "2026-10-08"})
        self.assertTrue(dk["issuable"])
        _, trace = self.req("POST", "/tasks/T1/delay-trace", {"delay_days": 5})
        self.assertEqual(trace["delay_days"], 5)

    def test_validation_errors_are_4xx(self):
        code, body = self.req("POST", "/tasks", {"task_id": "BAD", "segment_id": "NOPE",
                                                 "duration_days": 1})
        self.assertEqual(code, 409)
        self.assertIn("error", body)
        self.assertEqual(self.req("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()

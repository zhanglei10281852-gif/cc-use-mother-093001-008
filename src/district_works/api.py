"""HTTP REST 接口（标准库，零依赖）。

幂等：写操作可携带 ``Idempotency-Key`` 头，重复回执返回首次结果。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .contracts import WorkTask
from .repository import Repository
from .service import ProjectService, ServiceError


def _make_handler(get_service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DistrictWorks/1.0"

        def log_message(self, fmt, *args):  # 安静一些
            pass

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ServiceError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ServiceError("请求体必须是 JSON 对象")
            return data

        def _receipt(self) -> str | None:
            return self.headers.get("Idempotency-Key") or self.headers.get("X-Idempotency-Key")

        # ---- 路由 --------------------------------------------------------
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            svc = get_service()
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            query = parse_qs(urlparse(self.path).query)
            try:
                if method == "GET" and not parts:
                    return self._send(200, {"service": "district-works",
                                            "endpoints": ENDPOINTS})
                if parts == ["health"]:
                    return self._send(200, {"status": "ok"})
                if method == "GET" and parts == ["schedule"]:
                    return self._send(200, svc.schedule_view())
                if method == "GET" and parts == ["status"]:
                    return self._send(200, svc.status_view())
                if method == "GET" and parts == ["access", "verification"]:
                    return self._send(200, svc.verify_access())
                if method == "GET" and parts == ["acceptances"]:
                    return self._send(200, svc.list_acceptances(query.get("task_id", [None])[0]))

                if method == "POST" and parts == ["segments"]:
                    b = self._body()
                    return self._send(201, svc.add_segment(b["segment_id"], b["name"],
                                                           float(b["roadway_width_m"])))
                if method == "POST" and parts == ["crews"]:
                    b = self._body()
                    return self._send(201, svc.add_crew(b["crew_id"], b["name"],
                                                        b.get("trade", "")))
                if method == "POST" and parts == ["protected-objects"]:
                    b = self._body()
                    return self._send(201, svc.add_protected_object(
                        b["object_id"], b["segment_id"], b["name"],
                        float(b.get("radius_m", 0))))
                if method == "POST" and parts == ["windows"]:
                    b = self._body()
                    return self._send(201, svc.add_window(
                        b["window_id"], b["segment_id"], b["start_date"], b["end_date"]))
                if method == "POST" and parts == ["milestones"]:
                    b = self._body()
                    return self._send(201, svc.add_milestone(
                        b["milestone_id"], b["name"], b["due_date"], b.get("task_id")))
                if method == "POST" and parts == ["access-commitments"]:
                    b = self._body()
                    return self._send(201, svc.set_access_commitment(
                        b["segment_id"], float(b["minimum_width_m"]),
                        bool(b["always_open"]), b.get("commitment_id", "")))
                if method == "POST" and parts == ["tasks"]:
                    b = self._body()
                    task = WorkTask(
                        b["task_id"], b["segment_id"], tuple(b.get("depends_on", [])),
                        int(b["duration_days"]), b.get("name", ""), b.get("crew_id"),
                        float(b.get("occupies_width_m", 0)),
                        bool(b.get("requires_closure", False)),
                        bool(b.get("heritage_sensitive", False)),
                        b.get("earliest_start"), b.get("latest_finish"))
                    return self._send(201, svc.add_task(task))

                if len(parts) == 3 and parts[0] == "tasks" and method == "POST":
                    task_id, action = parts[1], parts[2]
                    b = self._body()
                    if action == "start":
                        return self._send(200, svc.start_task(task_id, self._receipt()))
                    if action == "acceptances":
                        return self._send(201, svc.record_acceptance(
                            task_id, b["scope"], float(b["quantity"]),
                            b.get("note", ""), b.get("recorder", ""), self._receipt()))
                    if action == "safe-resume":
                        return self._send(200, svc.safe_resume(
                            task_id, b.get("safety_checks", []),
                            b.get("resume_date"), self._receipt()))
                    if action == "delay-trace":
                        return self._send(200, svc.trace_delay(
                            task_id, int(b.get("delay_days", query.get("days", ["0"])[0]))))
                if method == "GET" and len(parts) == 4 and parts[0] == "tasks" \
                        and parts[2] == "acceptances":
                    return self._send(200, svc.list_acceptances(parts[1]))

                if method == "POST" and parts == ["incidents"]:
                    b = self._body()
                    return self._send(201, svc.report_discovery(
                        b["task_id"], b["kind"], b.get("note", ""), self._receipt()))

                if method == "POST" and parts == ["proposals"]:
                    b = self._body()
                    return self._send(201, svc.propose_change(
                        b["kind"], b.get("reason", ""), b.get("changes", {}),
                        self._receipt()))
                if method == "POST" and len(parts) == 3 and parts[0] == "proposals" \
                        and parts[2] == "decision":
                    b = self._body()
                    return self._send(200, svc.decide_proposal(
                        parts[1], b["decision"], b.get("approver", ""),
                        b["level"], b.get("note", ""), self._receipt()))

                if method == "POST" and parts == ["baselines"]:
                    b = self._body()
                    return self._send(201, svc.create_baseline(
                        b.get("name", "baseline"), self._receipt()))
                if method == "GET" and len(parts) == 3 and parts[0] == "baselines" \
                        and parts[2] == "comparison":
                    return self._send(200, svc.compare_baseline(parts[1]))

                if method == "POST" and parts == ["dockets"]:
                    b = self._body()
                    return self._send(201, svc.issue_daily_docket(
                        b["plan_date"], b.get("issued_by", "调度室"), self._receipt()))

                if method == "POST" and parts == ["advance"]:
                    b = self._body()
                    return self._send(200, svc.set_as_of(b["date"]))

                if method == "GET" and len(parts) == 2 and parts[0] == "receipts":
                    receipt = svc.repo.get_receipt(parts[1])
                    if receipt is None:
                        return self._send(404, {"error": "回执不存在"})
                    return self._send(200, receipt)

                return self._send(404, {"error": f"未找到路由: {method} {self.path}"})
            except ServiceError as exc:
                return self._send(409, {"error": str(exc)})
            except KeyError as exc:
                return self._send(400, {"error": f"缺少必填字段: {exc}"})
            except (ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})

    return Handler


ENDPOINTS = [
    "POST /segments /crews /protected-objects /windows /milestones "
    "/access-commitments /tasks",
    "GET /schedule /status /access/verification /acceptances",
    "POST /tasks/{id}/start|acceptances|safe-resume|delay-trace",
    "POST /incidents /proposals /proposals/{id}/decision",
    "POST /baselines /dockets /advance",
    "GET /baselines/{id}/comparison /receipts/{id}",
]


def build_server(host: str = "127.0.0.1", port: int = 8080,
                 db_path: str | None = None,
                 project_start: str | None = None) -> ThreadingHTTPServer:
    db_path = db_path or os.environ.get("DW_DB", ":memory:")
    ps = project_start or os.environ.get("DW_PROJECT_START")
    holder: dict = {}

    def get_service() -> ProjectService:
        if "svc" not in holder:
            holder["repo"] = Repository(db_path)
            holder["svc"] = ProjectService(holder["repo"], ps)
        return holder["svc"]

    handler = _make_handler(get_service)
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="历史街区施工协同后端")
    parser.add_argument("--host", default=os.environ.get("DW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DW_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("DW_DB", "district_works.db"))
    parser.add_argument("--project-start", default=os.environ.get("DW_PROJECT_START"))
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port, args.db, args.project_start)
    print(f"施工协同后端监听 http://{args.host}:{args.port}  数据库={args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

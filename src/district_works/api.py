"""HTTP REST 接口（仅用标准库）。

启动：python -m district_works.api --port 8080 --store ./data/events.jsonl

所有写操作可在 JSON 体内传 "command_id" 实现幂等重试。
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import DomainError
from .service import ConstructionService
from .store import EventStore


def _domain_error_payload(exc: DomainError) -> tuple[int, dict]:
    args = exc.args[0] if exc.args else {}
    if isinstance(args, dict):
        return 409, {"error": "domain_conflict", "details": args}
    return 422, {"error": "domain_error", "message": str(args)}


class Handler(BaseHTTPRequestHandler):
    server_version = "DistrictWorks/1.0"

    # ---- 工具 ----
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _svc(self) -> ConstructionService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # 静默
        pass

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            q = parse_qs(url.query)
            svc = self._svc()

            if parts == ["state"]:
                return self._send(200, svc.state_snapshot())
            if parts == ["access"]:
                return self._send(200, svc.verify_access(
                    through_date=(q.get("through") or [None])[0]))
            if parts == ["conflicts"]:
                return self._send(200, {"conflicts": svc.conflicts(
                    start_date=(q.get("start") or [None])[0])})
            if parts[:1] == ["delay-trace"] and len(parts) == 2:
                body = svc.trace_delay(
                    parts[1],
                    start_date=(q.get("start") or [None])[0],
                    delay_days=int(q["delay"][0]) if q.get("delay") else None,
                    duration_override=(int(q["duration"][0])
                                       if q.get("duration") else None))
                return self._send(200, body)
            if parts[:1] == ["changes"] and len(parts) == 2:
                snap = svc.state_snapshot()["changes"].get(parts[1])
                return self._send(200 if snap else 404,
                                  snap or {"error": "not_found"})
            if parts[:1] == ["events"]:
                events = [json.loads(e.to_line()) for e in svc.store.all_events()]
                return self._send(200, {"events": events,
                                        "count": len(events)})
            return self._send(404, {"error": "not_found", "path": self.path})
        except DomainError as exc:
            status, body = _domain_error_payload(exc)
            self._send(status, body)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            body = self._read_json()
            svc = self._svc()
            cid = body.get("command_id")

            if parts == ["segments"]:
                r = svc.register_segment(
                    body["segment_id"], body["name"], body["width_m"],
                    tuple(body.get("zone_ids", [])),
                    body.get("heritage_control", False), cid)
            elif parts == ["crews"]:
                r = svc.register_crew(
                    body["crew_id"], body["name"],
                    tuple(body.get("skills", [])), cid)
            elif parts == ["protections"]:
                r = svc.register_protection(
                    body["object_id"], body["segment_id"], body["kind"],
                    body.get("name", ""), body.get("buffer_m", 0.0),
                    body.get("required_level", 2), cid)
            elif parts == ["commitments"]:
                r = svc.add_access_commitment(
                    body["segment_id"], body["minimum_width_m"],
                    body.get("always_open", False), cid)
            elif parts == ["closure-windows"]:
                r = svc.declare_closure_window(
                    body["segment_id"], body["start_date"],
                    body["end_date"], cid)
            elif parts == ["open-windows"]:
                r = svc.declare_open_window(
                    body["segment_id"], body["date"],
                    body.get("work_allowed", False),
                    body.get("note", ""), cid)
            elif parts == ["milestones"]:
                r = svc.define_milestone(
                    body["milestone_id"], body["name"], body["due_date"],
                    tuple(body.get("task_ids", [])), cid)
            elif parts == ["tasks"]:
                r = svc.register_task(
                    body["task_id"], body["segment_id"],
                    tuple(body.get("depends_on", [])), body["duration_days"],
                    body.get("crew_id"), body.get("occupancy_width_m", 0.0),
                    body.get("requires_closure", False),
                    body.get("earliest_start"), cid)
            elif parts == ["schedule"]:
                r = svc.publish_schedule(
                    body["start_date"], cid,
                    allow_conflicts=body.get("allow_conflicts", False))
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "suspend":
                r = svc.suspend_task(parts[1], body["reason"],
                                     body.get("discovered"), cid)
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "resume-request":
                r = svc.request_resume(parts[1], body["safety_checks"],
                                      body["commander"], cid)
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "resume-approve":
                r = svc.approve_resume(parts[1], body["approver"], cid)
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "progress":
                r = svc.record_progress(parts[1], body["progress_days"], cid)
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "complete":
                r = svc.complete_task(parts[1], cid)
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "acceptance":
                r = svc.record_acceptance(
                    parts[1], body["inspector"], body["result"],
                    body.get("notes", ""), body.get("attachments"), cid)
            elif parts == ["changes"]:
                r = svc.submit_change(
                    body["kind"], body["payload"], body["summary"],
                    body["requested_by"], cid)
            elif len(parts) == 3 and parts[0] == "changes" \
                    and parts[2] == "decision":
                r = svc.decide_change(
                    parts[1], body["approved"], body["approver"],
                    body["level"], body.get("rationale", ""), cid)
            elif parts == ["baselines"]:
                r = svc.take_baseline(body["label"], cid)
            elif parts == ["daily-plans"]:
                r = svc.issue_daily_plan(body["date"], body["commander"], cid)
            else:
                return self._send(404, {"error": "not_found",
                                        "path": self.path})
            status = 200 if r.get("idempotent_replay") else 201
            return self._send(status, r)
        except DomainError as exc:
            status, body = _domain_error_payload(exc)
            self._send(status, body)
        except KeyError as exc:
            self._send(400, {"error": "missing_field", "field": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(exc)})


def build_server(store_path: str, port: int = 8080,
                 host: str = "127.0.0.1") -> ThreadingHTTPServer:
    store = EventStore(store_path)
    service = ConstructionService(store)
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service            # type: ignore[attr-defined]
    server.store = store                # type: ignore[attr-defined]
    # 串行化写操作，保证命令处理期间的"校验-落盘"原子语义
    server.service_lock = threading.RLock()  # type: ignore[attr-defined]
    _wrap_service_with_lock(server)
    return server


def _wrap_service_with_lock(server: ThreadingHTTPServer) -> None:
    lock = server.service_lock        # type: ignore[attr-defined]
    svc: ConstructionService = server.service  # type: ignore[attr-defined]
    for name in dir(svc):
        if name.startswith("_"):
            continue
        attr = getattr(svc, name)
        if callable(attr):
            def make(fn):
                def wrapped(*a, **kw):
                    with lock:
                        return fn(*a, **kw)
                return wrapped
            setattr(svc, name, make(attr))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史街区施工协同后端")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--store", default="data/events.jsonl")
    args = parser.parse_args(argv)
    server = build_server(args.store, args.port, args.host)
    print(f"施工协同服务已启动: http://{args.host}:{args.port} "
          f"(事件日志 {args.store})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

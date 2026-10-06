"""命令行管理工具：建模、排程、停工/变更/复工、日计划、基线、延期追溯。

示例：
  python -m district_works.cli --db demo.db init-demo
  python -m district_works.cli --db demo.db schedule
  python -m district_works.cli --db demo.db trace W-2 --days 3
  python -m district_works.cli --db demo.db access
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from .contracts import WorkTask
from .repository import Repository
from .service import ProjectService, ServiceError


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _svc(args) -> ProjectService:
    repo = Repository(args.db)
    return ProjectService(repo, getattr(args, "project_start", None))


# ---------------------------------------------------------------------------
# 演示场景
# ---------------------------------------------------------------------------
def cmd_init_demo(args) -> None:
    repo = Repository(args.db)
    start = args.project_start or date.today().isoformat()
    svc = ProjectService(repo, start)

    svc.add_segment("SEG-1", "青石板街北段", 6.0)
    svc.add_segment("SEG-2", "青石板街南段", 5.0)
    svc.add_segment("SEG-3", "文庙前街", 7.0)
    svc.add_crew("CREW-A", "市政管网一队", "管网")
    svc.add_crew("CREW-B", "市政管网二队", "管网")
    svc.add_crew("CREW-C", "道路恢复队", "道路")
    svc.add_protected_object("PO-1", "SEG-2", "明代砖砌排水沟遗迹", 5.0)
    svc.add_protected_object("PO-2", "SEG-3", "文庙照壁", 8.0)

    # 通行承诺：南段、文庙前街始终保留居民通道；北段仅在非封路日要求 1.2m
    svc.set_access_commitment("SEG-1", 1.2, False)
    svc.set_access_commitment("SEG-2", 1.5, True)
    svc.set_access_commitment("SEG-3", 2.0, True)

    d = lambda n: (date.fromisoformat(start) + timedelta(days=n)).isoformat()
    # 封路窗口很短：任何延误都会让窗口失效
    svc.add_window("WIN-1", "SEG-1", d(0), d(2))  # 窗口紧贴 W-1
    svc.add_window("WIN-2", "SEG-2", d(2), d(13))
    svc.add_window("WIN-3", "SEG-3", d(8), d(18))

    # 北段：封路窗口内全断面施工（承诺不要求始终开放）
    svc.add_task(WorkTask("W-1", "SEG-1", (), 3, "北段污水主管", "CREW-A",
                          occupies_width_m=3.0, requires_closure=True))
    # 南段紧邻文保遗迹且承诺始终保留 1.5m：只能半幅开挖，占 3.5m
    svc.add_task(WorkTask("W-2", "SEG-2", ("W-1",), 4, "南段污水主管", "CREW-A",
                          occupies_width_m=3.5, requires_closure=False,
                          heritage_sensitive=True))
    svc.add_task(WorkTask("W-3", "SEG-2", ("W-2",), 2, "南段雨水管", "CREW-B",
                          occupies_width_m=3.0))
    # 文庙前街承诺始终留 2m：占 4.5m 半幅施工，由完工后的管网一队接续
    svc.add_task(WorkTask("W-4", "SEG-3", ("W-2",), 3, "文庙前街雨污分流", "CREW-A",
                          occupies_width_m=4.5, requires_closure=False,
                          heritage_sensitive=True, latest_finish=d(18)))
    svc.add_task(WorkTask("W-5", "SEG-1", ("W-1",), 2, "北段路面恢复", "CREW-C",
                          occupies_width_m=2.0))
    svc.add_task(WorkTask("W-6", "SEG-2", ("W-3",), 2, "南段路面恢复", "CREW-C",
                          occupies_width_m=3.0))

    svc.add_milestone("MS-1", "管网贯通（雨污分流完成）", d(8), "W-3")
    svc.add_milestone("MS-2", "文庙前街完工", d(9), "W-4")
    _print(svc.create_baseline("初始基线"))
    print(f"演示工程已建立：项目开工日 {start}", file=sys.stderr)


# ---------------------------------------------------------------------------
def cmd_serve(args) -> None:
    from .api import build_server
    server = build_server(args.host, args.port, args.db, args.project_start)
    print(f"施工协同后端监听 http://{host_port(server)}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def host_port(server) -> str:
    return f"{server.server_address[0]}:{server.server_address[1]}"


def cmd_schedule(args) -> None:
    _print(_svc(args).schedule_view())


def cmd_status(args) -> None:
    _print(_svc(args).status_view())


def cmd_access(args) -> None:
    _print(_svc(args).verify_access())


def cmd_start(args) -> None:
    _print(_svc(args).start_task(args.task_id, args.receipt))


def cmd_incident(args) -> None:
    _print(_svc(args).report_discovery(args.task_id, args.kind, args.note, args.receipt))


def cmd_propose_task(args) -> None:
    changes = {"task_id": args.task_id}
    if args.duration is not None:
        changes["duration_days"] = args.duration
    if args.crew:
        changes["crew_id"] = args.crew
    if args.closure is not None:
        changes["requires_closure"] = args.closure
    if args.width is not None:
        changes["occupies_width_m"] = args.width
    _print(_svc(args).propose_change("TASK_UPDATE", args.reason, changes, args.receipt))


def cmd_propose_resume(args) -> None:
    changes = {"incident_id": args.incident_id, "extra_days": args.extra_days}
    if args.resume_date:
        changes["resume_date"] = args.resume_date
    _print(_svc(args).propose_change("RESUME_AFTER_DISCOVERY", args.reason,
                                     changes, args.receipt))


def cmd_propose_window(args) -> None:
    changes = {"window_id": args.window_id}
    if args.start:
        changes["start_date"] = args.start
    if args.end:
        changes["end_date"] = args.end
    _print(_svc(args).propose_change("WINDOW_SHIFT", args.reason, changes, args.receipt))


def cmd_decide(args) -> None:
    _print(_svc(args).decide_proposal(args.proposal_id, args.decision,
                                      args.approver, args.level, args.note,
                                      args.receipt))


def cmd_resume(args) -> None:
    _print(_svc(args).safe_resume(args.task_id, args.checks, args.resume_date,
                                  args.receipt))


def cmd_accept(args) -> None:
    _print(_svc(args).record_acceptance(args.task_id, args.scope, args.quantity,
                                        args.note, args.recorder, args.receipt))


def cmd_baseline(args) -> None:
    _print(_svc(args).create_baseline(args.name, args.receipt))


def cmd_baseline_diff(args) -> None:
    _print(_svc(args).compare_baseline(args.baseline_id))


def cmd_docket(args) -> None:
    _print(_svc(args).issue_daily_docket(args.date, args.issued_by, args.receipt))


def cmd_trace(args) -> None:
    _print(_svc(args).trace_delay(args.task_id, args.days))


def cmd_advance(args) -> None:
    _print(_svc(args).set_as_of(args.date))


def cmd_receipt(args) -> None:
    svc = _svc(args)
    receipt = svc.repo.get_receipt(args.receipt_id)
    if receipt is None:
        raise SystemExit("回执不存在")
    _print(receipt)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="district-works", description="历史街区施工协同后端")
    p.add_argument("--db", default=":memory:", help="SQLite 路径（默认内存库）")
    p.add_argument("--project-start", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init-demo"); sp.set_defaults(func=cmd_init_demo)

    sp = sub.add_parser("serve"); sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080); sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("schedule"); sp.set_defaults(func=cmd_schedule)
    sp = sub.add_parser("status"); sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("access"); sp.set_defaults(func=cmd_access)

    sp = sub.add_parser("start"); sp.add_argument("task_id")
    _add_receipt(sp); sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("incident")
    sp.add_argument("task_id")
    sp.add_argument("kind", choices=["UNKNOWN_UTILITY", "PROTECTED_OBJECT"])
    sp.add_argument("note", nargs="?", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_incident)

    sp = sub.add_parser("propose-task")
    sp.add_argument("task_id"); sp.add_argument("--duration", type=int)
    sp.add_argument("--crew"); sp.add_argument("--width", type=float)
    sp.add_argument("--closure", dest="closure", action="store_true", default=None)
    sp.add_argument("--reason", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_propose_task)

    sp = sub.add_parser("propose-resume")
    sp.add_argument("incident_id"); sp.add_argument("--extra-days", type=int, default=0)
    sp.add_argument("--resume-date"); sp.add_argument("--reason", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_propose_resume)

    sp = sub.add_parser("propose-window")
    sp.add_argument("window_id"); sp.add_argument("--start"); sp.add_argument("--end")
    sp.add_argument("--reason", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_propose_window)

    sp = sub.add_parser("decide")
    sp.add_argument("proposal_id")
    sp.add_argument("decision", choices=["APPROVED", "REJECTED"])
    sp.add_argument("--level", required=True, choices=["SITE", "OFFICE", "HERITAGE"])
    sp.add_argument("--approver", default="")
    sp.add_argument("--note", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_decide)

    sp = sub.add_parser("resume")
    sp.add_argument("task_id")
    sp.add_argument("--checks", nargs="+", required=True)
    sp.add_argument("--resume-date")
    _add_receipt(sp); sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("accept")
    sp.add_argument("task_id")
    sp.add_argument("--scope", choices=["PARTIAL", "FULL"], required=True)
    sp.add_argument("--quantity", type=float, required=True)
    sp.add_argument("--note", default=""); sp.add_argument("--recorder", default="")
    _add_receipt(sp); sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("baseline"); sp.add_argument("name", nargs="?", default="基线")
    _add_receipt(sp); sp.set_defaults(func=cmd_baseline)

    sp = sub.add_parser("baseline-diff"); sp.add_argument("baseline_id")
    sp.set_defaults(func=cmd_baseline_diff)

    sp = sub.add_parser("docket"); sp.add_argument("date")
    sp.add_argument("--issued-by", default="调度室")
    _add_receipt(sp); sp.set_defaults(func=cmd_docket)

    sp = sub.add_parser("trace"); sp.add_argument("task_id")
    sp.add_argument("--days", type=int, required=True)
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("advance"); sp.add_argument("date")
    sp.set_defaults(func=cmd_advance)

    sp = sub.add_parser("receipt"); sp.add_argument("receipt_id")
    sp.set_defaults(func=cmd_receipt)
    return p


def _add_receipt(sp) -> None:
    sp.add_argument("--receipt", default=None, help="客户端幂等回执号")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ServiceError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

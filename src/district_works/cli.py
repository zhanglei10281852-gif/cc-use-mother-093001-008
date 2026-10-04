"""命令行工具：管理人员可直接追溯延期传播、验证通行承诺、处理停工与变更。

用法示例：
  python -m district_works.cli --store data/events.jsonl init-demo
  python -m district_works.cli --store data/events.jsonl trace W-3 --delay 3
  python -m district_works.cli --store data/events.jsonl access
  python -m district_works.cli --store data/events.jsonl state
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .domain import DomainError
from .service import APPROVAL_LEVELS, ConstructionService
from .store import EventStore


def _svc(args: argparse.Namespace) -> ConstructionService:
    return ConstructionService(EventStore(args.store))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _ok(exc_fn) -> int:
    try:
        _print(exc_fn())
        return 0
    except DomainError as exc:
        payload = exc.args[0] if exc.args else str(exc)
        print(json.dumps({"error": "domain_conflict", "details": payload},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


# ================= 演示场景 =================

def init_demo(svc: ConstructionService) -> dict:
    """构建一个可演示全部机制的街区场景（命令自带固定 command_id，可安全重跑）。"""
    cid = lambda n: f"demo-{n}"  # noqa: E731
    svc.register_segment("SEG-1", "东街口", 4.0, ("ZONE-A",), False, cid("seg1"))
    svc.register_segment("SEG-2", "古树巷", 3.0, ("ZONE-A",), True, cid("seg2"))
    svc.register_segment("SEG-3", "河边街", 5.0, ("ZONE-B",), False, cid("seg3"))

    svc.register_crew("CREW-甲", "甲班(管网)", ("sewer",), cid("crew1"))
    svc.register_crew("CREW-乙", "乙班(路面)", ("pavement",), cid("crew2"))

    svc.register_protection("P-1", "SEG-2", "heritage", "百年古樟",
                            buffer_m=2.0, required_level=3,
                            command_id=cid("p1"))
    svc.register_protection("P-2", "SEG-1", "utility", "已知燃气支管",
                            buffer_m=1.0, required_level=2,
                            command_id=cid("p2"))

    # SEG-2 是居民出入通道：承诺始终保持 1.5m 通行
    svc.add_access_commitment("SEG-2", 1.5, True, cid("acc2"))
    svc.add_access_commitment("SEG-1", 1.2, False, cid("acc1"))

    svc.declare_closure_window("SEG-1", "2026-11-02", "2026-11-20", cid("cw1"))

    # 每周三 SEG-2 集市必须保持开放
    for d in ("2026-11-04", "2026-11-11", "2026-11-18", "2026-11-25"):
        svc.declare_open_window("SEG-2", d, False, "集市日", cid(f"mkt-{d}"))

    t = lambda tid, seg, deps, dur, crew, occ=0.0, closure=False, early=None, n="": \
        svc.register_task(tid, seg, tuple(deps), dur, crew, occ, closure,
                          early, command_id=cid(f"task-{tid}"))
    t("W-1", "SEG-1", (), 3, "CREW-甲", 1.5)
    t("W-2", "SEG-2", ("W-1",), 4, "CREW-甲", 1.2)
    t("W-3", "SEG-1", ("W-1",), 3, "CREW-乙", 2.0, closure=True,
      early="2026-11-02")
    t("W-4", "SEG-3", ("W-2",), 2, "CREW-乙", 1.0)
    t("W-5", "SEG-2", ("W-2", "W-3"), 2, "CREW-甲", 1.0)

    svc.define_milestone("M-1", "主干管贯通", "2026-11-25",
                         ("W-1", "W-2", "W-5"), cid("ms1"))

    pub = svc.publish_schedule("2026-11-02", cid("pub"))
    base = svc.take_baseline("开工基线", cid("base"))
    return {"schedule": pub["assignments"],
            "conflicts": pub["conflicts"],
            "access": pub["access"],
            "baseline_id": base["baseline_id"]}


# ================= 子命令 =================

def cmd_init_demo(args) -> int:
    return _ok(lambda: init_demo(_svc(args)))


def cmd_state(args) -> int:
    return _ok(lambda: _svc(args).state_snapshot())


def cmd_events(args) -> int:
    svc = _svc(args)
    for e in svc.store.all_events():
        print(e.to_line())
    return 0


def cmd_access(args) -> int:
    return _ok(lambda: _svc(args).verify_access(args.through))


def cmd_conflicts(args) -> int:
    return _ok(lambda: {"conflicts": _svc(args).conflicts(args.start)})


def cmd_trace(args) -> int:
    def go():
        r = _svc(args).trace_delay(
            args.task, start_date=args.start, delay_days=args.delay,
            duration_override=args.duration)
        if args.summary:
            return _trace_summary(args.task, r)
        return r
    return _ok(go)


def _trace_summary(task_id: str, r: dict) -> dict:
    rows = []
    for c in r["propagation_chain"]:
        if c.get("unschedulable"):
            rows.append(f"  {c['task_id']}({c['segment_id']}): "
                        f"原计划 {c['baseline_start']} → 无法排程"
                        f"（{c.get('blocking_conflict', {}).get('message', '约束冲突')}）")
            continue
        tag = "根因" if c["root"] else f"顺延 {c['shift_days']}天"
        rows.append(f"  {c['task_id']}({c['segment_id']}): "
                    f"{c['baseline_start']} → {c['scenario_start']} ({tag})"
                    + ("；原因: " + "；".join(c["causes"]) if c["causes"] else ""))
    ms = [m for m in r["milestone_impact"] if m["breached"]]
    print(f"延期传播追溯（触发: {task_id}，{'; '.join(r['triggers'])}）")
    print("\n".join(rows) or "  无下游任务")
    if ms:
        for m in ms:
            print(f"  ⚠ 里程碑 {m['milestone_id']}({m['name']}) 预计 "
                  f"{m['forecast_end']} 完成，晚于截止 {m['due_date']}")
    else:
        print("  里程碑均不受影响")
    return {"propagated_tasks": [c["task_id"] for c in r["propagation_chain"]],
            "unschedulable": [c["task_id"] for c in r["propagation_chain"]
                              if c.get("unschedulable")],
            "milestones_breached": [m["milestone_id"] for m in ms]}


def cmd_suspend(args) -> int:
    discovered = json.loads(args.discovered) if args.discovered else None
    return _ok(lambda: _svc(args).suspend_task(
        args.task, args.reason, discovered, args.command_id))


def cmd_resume_request(args) -> int:
    checks: dict[str, bool] = {}
    for item in args.checks.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, flag = item.split(":", 1)
            checks[name.strip()] = flag.strip().lower() in (
                "true", "1", "yes", "满足", "是")
        else:
            checks[item] = True
    return _ok(lambda: _svc(args).request_resume(
        args.task, checks, args.commander, args.command_id))


def cmd_resume_approve(args) -> int:
    return _ok(lambda: _svc(args).approve_resume(
        args.task, args.approver, args.command_id))


def cmd_progress(args) -> int:
    return _ok(lambda: _svc(args).record_progress(
        args.task, args.days, args.command_id))


def cmd_complete(args) -> int:
    return _ok(lambda: _svc(args).complete_task(args.task, args.command_id))


def cmd_accept(args) -> int:
    return _ok(lambda: _svc(args).record_acceptance(
        args.task, args.inspector, args.result, args.notes or "",
        None, args.command_id))


def cmd_change(args) -> int:
    payload: dict = {}
    if args.task:
        payload["task_id"] = args.task
    if args.duration:
        payload["duration_days"] = args.duration
    if args.new_start:
        payload["new_start"] = args.new_start
    if args.delay_days is not None:
        payload["delay_days"] = args.delay_days
    if args.closure_segment:
        payload["segment_id"] = args.closure_segment
        payload["start_date"] = args.closure_start
        payload["end_date"] = args.closure_end
    if args.scope_segments:
        payload["segment_ids"] = args.scope_segments.split(",")

    def go():
        r = _svc(args).submit_change(
            args.kind, payload, args.summary or "(无摘要)",
            args.by, args.command_id)
        print(f"变更单 {r['change_id']} 需要审批级别 "
              f"{r['required_level']} 级（{r['required_role']}）",
              file=sys.stderr)
        print(f"受影响任务: {r['affected_tasks']}", file=sys.stderr)
        if r["conflicts"]:
            print(f"冲突: {json.dumps(r['conflicts'], ensure_ascii=False)}",
                  file=sys.stderr)
        if r["access_violations"]:
            print("通行承诺冲突: "
                  + json.dumps(r["access_violations"], ensure_ascii=False),
                  file=sys.stderr)
        return r
    return _ok(go)


def cmd_decide(args) -> int:
    return _ok(lambda: _svc(args).decide_change(
        args.change_id, args.approved, args.approver, args.level,
        args.rationale or "", args.command_id))


def cmd_baseline(args) -> int:
    return _ok(lambda: _svc(args).take_baseline(args.label, args.command_id))


def cmd_daily(args) -> int:
    return _ok(lambda: _svc(args).issue_daily_plan(
        args.date, args.commander, args.command_id))


def cmd_serve(args) -> int:
    from .api import build_server
    server = build_server(args.store, args.port, args.host)
    print(f"施工协同服务已启动: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="历史街区施工协同命令行")
    p.add_argument("--store", default="data/events.jsonl",
                   help="事件日志路径（重启后自动重放恢复状态）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-demo").set_defaults(fn=cmd_init_demo)
    sub.add_parser("state").set_defaults(fn=cmd_state)
    sub.add_parser("events").set_defaults(fn=cmd_events)

    sp = sub.add_parser("access"); sp.add_argument("--through")
    sp.set_defaults(fn=cmd_access)

    sp = sub.add_parser("conflicts"); sp.add_argument("--start")
    sp.set_defaults(fn=cmd_conflicts)

    sp = sub.add_parser("trace", help="追溯一次延期如何传播")
    sp.add_argument("task"); sp.add_argument("--delay", type=int)
    sp.add_argument("--duration", type=int)
    sp.add_argument("--start")
    sp.add_argument("--summary", action="store_true", help="输出可读摘要")
    sp.set_defaults(fn=cmd_trace)

    sp = sub.add_parser("suspend", help="现场停工上报（未知管线/保护对象）")
    sp.add_argument("task"); sp.add_argument("--reason", required=True)
    sp.add_argument("--discovered", help='JSON, 如 {"kind":"utility","name":"未知排水管"}')
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_suspend)

    sp = sub.add_parser("resume-request", help="申请安全复工")
    sp.add_argument("task"); sp.add_argument("--checks", required=True,
                                             help="逗号分隔的安全条件，全部须满足")
    sp.add_argument("--commander", required=True)
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_resume_request)

    sp = sub.add_parser("resume-approve")
    sp.add_argument("task"); sp.add_argument("--approver", required=True)
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_resume_approve)

    sp = sub.add_parser("progress", help="部分完工登记工日")
    sp.add_argument("task"); sp.add_argument("--days", type=int, required=True)
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_progress)

    sp = sub.add_parser("complete")
    sp.add_argument("task"); sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_complete)

    sp = sub.add_parser("accept", help="验收（记录不可覆盖）")
    sp.add_argument("task"); sp.add_argument("--inspector", required=True)
    sp.add_argument("--result", required=True)
    sp.add_argument("--notes")
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_accept)

    sp = sub.add_parser("change", help="提交变更（自动重评受影响任务与审批级别）")
    sp.add_argument("--kind", default="task_update",
                    choices=["task_update", "closure_extension",
                             "scope_change", "other"])
    sp.add_argument("--task"); sp.add_argument("--duration", type=int)
    sp.add_argument("--new-start", dest="new_start")
    sp.add_argument("--delay-days", dest="delay_days", type=int)
    sp.add_argument("--closure-segment", dest="closure_segment")
    sp.add_argument("--closure-start", dest="closure_start")
    sp.add_argument("--closure-end", dest="closure_end")
    sp.add_argument("--scope-segments", dest="scope_segments")
    sp.add_argument("--summary"); sp.add_argument("--by", required=True)
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_change)

    sp = sub.add_parser("decide", help="按级别批准/驳回变更")
    sp.add_argument("change_id"); sp.add_argument("--approver", required=True)
    sp.add_argument("--level", type=int, required=True,
                    help="1=现场主管 2=项目经理 3=街区更新办公室(会同文保)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", dest="approved", action="store_true")
    g.add_argument("--reject", dest="approved", action="store_false")
    sp.add_argument("--rationale")
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_decide)

    sp = sub.add_parser("baseline")
    sp.add_argument("label"); sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_baseline)

    sp = sub.add_parser("daily", help="签发日计划")
    sp.add_argument("date"); sp.add_argument("--commander", required=True)
    sp.add_argument("--command-id", dest="command_id")
    sp.set_defaults(fn=cmd_daily)

    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--host", default="127.0.0.1")
    sp.set_defaults(fn=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

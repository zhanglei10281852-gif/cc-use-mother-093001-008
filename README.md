# 历史街区地下施工协同后端

把散落各处的**作业区段、工序依赖、保护对象、通行承诺、封路窗口与审批条件**纳入同一计划，
在排程时识别资源、空间、通行与开放时段冲突；支持现场停工上报、变更分级审批、安全复工、
部分完工、里程碑基线、日计划签发、幂等回执与服务重启状态恢复。

纯 Python 标准库实现（领域模型 + 排程引擎 + SQLite 持久化 + REST API + CLI），零第三方依赖。

## 运行

```bash
python -m unittest discover -s tests -v          # 测试（46 个）
python -m compileall -q src tests run_cli.py     # 编译检查
python run_cli.py                                # 基础契约冒烟
PYTHONPATH=src python -m district_works.cli --db demo.db --project-start 2026-10-08 init-demo
PYTHONPATH=src python -m district_works.cli --db demo.db serve --port 8080
```

## 模块

| 文件 | 职责 |
|---|---|
| `contracts.py` | `WorkTask` / `AccessCommitment` 基础契约（保持原始签名向后兼容） |
| `models.py` | 区段、队伍、保护对象、封路窗口、里程碑、停工事件、变更方案、验收、基线、日计划 |
| `schedule.py` | 拓扑正排；资源/空间/通行/开放时段/里程碑冲突识别；停工阻断传播；延期追溯；通行承诺逐日核验 |
| `service.py` | 业务状态机：开工、部分完工、停工上报、变更评估分级审批、安全复工、基线、日计划、幂等 |
| `repository.py` | SQLite 持久化；**验收/基线/回执仅追加**（触发器拒绝 UPDATE/DELETE） |
| `api.py` | HTTP REST（`Idempotency-Key` 头实现幂等） |
| `cli.py` | 命令行管理 + 可复现的街区演示场景 |

## 核心规则

- **冲突识别**：同一施工队同日多任务（RESOURCE）、同区段作业面叠加超路宽（SPACE）、
  居民通道余宽低于承诺或“始终开放”区段被全断面封路（ACCESS）、
  封路作业超出批准窗口或最晚完工日（OPEN_HOURS）、里程碑基线被突破（MILESTONE）。
  经批准的封路窗口内可全断面施工并豁免余宽，但 `always_open` 承诺在任何日期都不允许封路。
- **停工上报**：发现未知管线/保护对象立即停工，任务及其全部下游在排程中阻断。
- **变更分级审批**：评估时先在试探计划上重算受影响任务与冲突，再定级——
  涉及保护对象/文保区段或发现保护对象需 **HERITAGE（文保主管部门）**；
  窗口调整、未知管线复工、突破里程碑需 **OFFICE（街区更新办公室）**；
  其余为 **SITE（现场级）**。低级别审批被拒绝。已完工验收的任务不可变更。
- **安全复工**：方案批准仅表示方案通过；现场还须提交安全核查项，系统重评无硬冲突后才能复工。
- **部分完工**：`PARTIAL` 验收按完成作业天数核减剩余工期；`FULL` 验收锁定为 COMPLETED。
- **验收不可变**：`acceptances` 表触发器拒绝任何 UPDATE/DELETE。
- **里程碑基线**：基线仅追加快照，可随时对比任务漂移与里程碑是否按期。
- **日计划签发**：按日期汇总当日作业与阻断；当日存在冲突时标记为不可签发。
- **幂等**：写操作携带回执号，重复回执返回首次结果（`idempotent_replay: true`），
  同一回执用于不同操作会被拒绝。
- **重启恢复**：停工、待审批、已完工/部分完工进度、实际完工日均持久化，重启后自动恢复。
- **延期追溯**：`trace` 模拟某任务延误，输出下游顺移、被突破的里程碑、新增通行违约与冲突。

## CLI 速览

```bash
cli schedule                      # 查看排程与全部冲突
cli access                        # 逐日核验居民通道承诺
cli start W-1 --receipt R-001     # 开工（幂等）
cli accept W-1 --scope PARTIAL --quantity 2
cli incident W-2 UNKNOWN_UTILITY "开挖面发现旧燃气管"
cli propose-resume INC-xxx --extra-days 3 --resume-date 2026-10-15
cli propose-window WIN-1 --end 2026-10-20 --reason 配合避让
cli decide PRP-xxx APPROVED --level HERITAGE --approver 文保局李
cli resume W-2 --checks 支护复核 管线探测 临时通道 --resume-date 2026-10-15
cli baseline 变更前基线
cli baseline-diff BL-xxx
cli docket 2026-10-15
cli advance 2026-10-11            # 推进计划日历日
cli trace W-1 --days 3            # 追溯延期传播
cli receipt R-001                 # 查询回执
```

## REST 速览

```
POST /segments /crews /protected-objects /windows /milestones /access-commitments /tasks
GET  /schedule /status /access/verification /acceptances
POST /tasks/{id}/start | /acceptances | /safe-resume | /delay-trace
POST /incidents
POST /proposals ; POST /proposals/{id}/decision
POST /baselines ; GET /baselines/{id}/comparison
POST /dockets ; POST /advance
GET  /receipts/{id}
```

写操作加 `Idempotency-Key: <唯一回执>` 头保证“重复回执保持幂等”。
业务规则冲突返回 `409 {"error": ...}`，参数错误返回 `400`，未知路由返回 `404`。

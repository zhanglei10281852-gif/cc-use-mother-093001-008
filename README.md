# 历史街区地下施工协同后端

将散落在不同表格中的**作业区段、工序依赖、保护对象、通行承诺、封路/开放窗口、
审批条件**纳入同一计划；排程时识别资源、空间与开放时段冲突，现场可停工上报，
变更走分级审批并自动重评受影响任务，验收记录不可覆盖，服务重启后恢复全部状态。

## 设计要点

- **事件溯源（JSONL 追加日志）**：所有状态变更落为不可变事件，
  `SUSPENDED`、`PENDING_APPROVAL` 等状态随服务重启重放恢复。
- **幂等命令回执**：写命令携带 `command_id`，重复提交（含跨进程重启）
  返回首次回执，不产生重复事件。
- **纯函数排程内核**（`scheduling.py`）：前向重排，逐日检查
  工序依赖、班组（资源）、空间组/区段（空间）、开放时段、封路窗口，
  推演过程完全确定、可复算、可追溯。
- **验收记录锁定**：已完成/已验收任务不能被后续改动覆盖，
  针对它们的变更与重复验收一律拒绝。
- **变更控制闭环**：提交即重评受影响任务（依赖下游 + 同班组/同空间）、
  冲突、通行承诺与里程碑影响，按级别审批（1 现场主管 / 2 项目经理 /
  3 街区更新办公室会同文保），批准后才应用并重排发布。
- **安全复工**：停工→逐项安全条件申请→安全员核实全部满足且通行不受影响→复工。

仅使用 Python 3.11 标准库，无第三方依赖。

## 测试与检查

```bash
python -m unittest discover -s tests -v     # 31 个测试
python -m compileall -q src tests run_cli.py
python run_cli.py                            # 早期契约冒烟
```

## 命令行

```bash
export PYTHONPATH=src
# 一键构建演示街区（三个区段、两个班组、文保古树、集市日、封路窗口、5 道工序）
python -m district_works.cli --store data/events.jsonl init-demo

# 追溯一次延期如何沿依赖/资源/空间传播，并核对里程碑
python -m district_works.cli --store data/events.jsonl trace W-1 --delay 3 --summary

# 验证居民/商户通道始终满足承诺（宽度 + 始终开放）
python -m district_works.cli --store data/events.jsonl access

# 现场发现未知管线：停工上报并自动登记保护对象
python -m district_works.cli --store data/events.jsonl suspend W-2 \
  --reason "开挖面发现未知陶土排水管" \
  --discovered '{"kind":"unknown","name":"陶土排水管","buffer_m":2.0}'

# 安全复工：条件须全部满足，再由安全员批准
python -m district_works.cli --store data/events.jsonl resume-request W-2 \
  --checks "管线交底:true,支护检查:true,通道恢复:true" --commander 李班
python -m district_works.cli --store data/events.jsonl resume-approve W-2 --approver 王安全

# 部分完工 / 完工 / 验收（验收记录不可覆盖）
python -m district_works.cli --store data/events.jsonl progress W-1 --days 1
python -m district_works.cli --store data/events.jsonl complete W-1
python -m district_works.cli --store data/events.jsonl accept W-1 --inspector 张监理 --result 合格

# 设计变更：自动给出受影响任务、所需级别、冲突与里程碑影响
python -m district_works.cli --store data/events.jsonl change \
  --task W-3 --duration 8 --by 设计方 --summary "顶管工艺调整"
python -m district_works.cli --store data/events.jsonl decide <变更单ID> \
  --approver 办公室 --level 3 --approve

# 里程碑基线、日计划签发、状态/事件追溯
python -m district_works.cli --store data/events.jsonl baseline 开工基线
python -m district_works.cli --store data/events.jsonl daily 2026-11-09 --commander 赵调度
python -m district_works.cli --store data/events.jsonl state
python -m district_works.cli --store data/events.jsonl events

# 启动 REST 服务（也可 python -m district_works.api --port 8080）
python -m district_works.cli --store data/events.jsonl serve --port 8080
```

## REST 接口

启动：`python -m district_works.api --port 8080 --store data/events.jsonl`

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /segments` `/crews` `/protections` `/commitments` | 区段、班组、保护对象、通行承诺 |
| `POST /closure-windows` `/open-windows` `/milestones` | 封路窗口、开放时段、里程碑 |
| `POST /tasks` | 登记工序（含依赖、班组、占用宽度、是否需封路） |
| `POST /schedule` | 前向排程发布（有硬冲突或通行违规时 409 阻断） |
| `POST /tasks/{id}/suspend` | 停工上报，可带新发现的保护对象 |
| `POST /tasks/{id}/resume-request` `/resume-approve` | 安全复工申请与批准 |
| `POST /tasks/{id}/progress` `/complete` `/acceptance` | 部分完工、完工、验收 |
| `POST /changes`，`POST /changes/{id}/decision` | 变更提交（重评影响/级别）与审批 |
| `POST /baselines` `/daily-plans` | 里程碑基线、日计划签发 |
| `GET /state` `/access` `/conflicts` `/events` | 状态快照、通行校核、冲突、事件流 |
| `GET /delay-trace/{task}?delay=3&duration=8` | 延期传播追溯（基线 vs 推演） |

写请求在 JSON 体内给 `"command_id": "..."` 即可安全重试；
重复请求返回 `idempotent_replay: true` 与同一份回执。

例：

```bash
curl -s -XPOST localhost:8080/tasks -d '{"task_id":"W-9","segment_id":"SEG-1",
  "depends_on":["W-8"],"duration_days":2,"crew_id":"CREW-甲","command_id":"w9"}'
```

## 排程冲突类型

| 冲突 | 含义 |
| --- | --- |
| `missing_segment` | 任务引用了未登记区段 |
| `access_width` | 作业占用后剩余通道宽度小于承诺最小值 |
| `unschedulable` | 视窗内找不到同时满足依赖/资源/空间/开放时段/封路窗口的连续工作日 |
| `dependency_blocked` | 前置任务排不下，依赖链断裂级联 |
| `closure_window` | 封路作业日落入批准窗口之外，封路窗口失效 |

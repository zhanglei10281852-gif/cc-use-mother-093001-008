"""历史街区施工协同领域包。

模块组成：
- contracts:  值对象（区段、任务、保护对象、通行承诺、封路/开放窗口、里程碑）
- events:     领域事件
- store:      追加式 JSONL 事件存储（幂等回执、崩溃重放）
- domain:     计划聚合与业务规则（验收锁定、变更门槛等）
- scheduling: 纯函数排程内核（资源/空间/开放时段/封路窗口冲突、延期传播、
              通行承诺校核、日计划）
- service:    应用服务（命令幂等、停工复工、变更分级审批、基线、日计划）
- api:        标准库 HTTP REST 接口
- cli:        命令行工具
"""
from .contracts import (AccessCommitment, ClosureWindow, Crew, Milestone,
                        OpenWindow, ProtectionObject, Segment, WorkTask)
from .domain import DomainError, Plan, TaskStatus
from .service import ConstructionService
from .store import EventStore

__all__ = [
    "AccessCommitment", "ClosureWindow", "Crew", "Milestone", "OpenWindow",
    "ProtectionObject", "Segment", "WorkTask",
    "DomainError", "Plan", "TaskStatus",
    "ConstructionService", "EventStore",
]

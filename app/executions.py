"""现场执行：执行单创建、扫描事件批次应用与状态查询。

规则概述：
- 仅 status=success 的方案可创建执行单，创建时冻结货物与舱室分配；
- 事件按 sequence 从 1 连续递增，新事件在同一事务中按序应用；
- 货物必须装入计划舱室，不得重复装载、未装先卸，开始卸载前须完成全部装载；
- 任一新事件违规则整批回滚，返回首个错误与提交前状态（HTTP 422）；
- 相同 event_id 且载荷相同的重试不重复推进；载荷不同返回 409；
- 全部货物卸完后执行单自动置为 completed。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Execution, ExecutionCargo, ExecutionEvent, Plan
from .schemas import (
    CargoStateOut,
    EventBatchIn,
    EventBatchResult,
    EventOut,
    ExecutionOut,
    ScanEventIn,
)

# 货物现场状态
PENDING, LOADED, UNLOADED = "pending", "loaded", "unloaded"
# 执行单状态
IN_PROGRESS, COMPLETED = "in_progress", "completed"

# 串行化事件批次写入，避免并发批次交错推进同一执行单
_events_lock = threading.Lock()


def natural_key(s: str):
    """编号排序键：纯数字按数值排序，其余按字符串排序，保证稳定一致。"""
    return (0, int(s)) if s.isdigit() else (1, s)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_execution(db: Session, execution_id: int) -> Execution:
    execution = db.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"执行单 {execution_id} 不存在")
    return execution


def _cargo_states(db: Session, execution_id: int) -> list[CargoStateOut]:
    """按货物编号稳定排序的当前货物状态。"""
    rows = (
        db.query(ExecutionCargo)
        .filter(ExecutionCargo.execution_id == execution_id)
        .all()
    )
    rows.sort(key=lambda r: natural_key(r.cargo_id))
    return [
        CargoStateOut(
            cargo_id=r.cargo_id,
            planned_compartment_id=r.planned_compartment_id,
            status=r.status,
        )
        for r in rows
    ]


def serialize_execution(db: Session, execution: Execution) -> ExecutionOut:
    """执行单完整视图：最后序号、每件货物状态与已应用事件日志。"""
    events = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_id == execution.id)
        .order_by(ExecutionEvent.sequence)
        .all()
    )
    return ExecutionOut(
        execution_id=execution.id,
        plan_id=execution.plan_id,
        status=execution.status,
        last_sequence=execution.last_sequence,
        cargos=_cargo_states(db, execution.id),
        events=[
            EventOut(
                event_id=e.event_id,
                sequence=e.sequence,
                cargo_id=e.cargo_id,
                action=e.action,
                compartment_id=e.compartment_id,
            )
            for e in events
        ],
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


def create_execution(db: Session, plan_id: int) -> ExecutionOut:
    """为 status=success 的方案创建执行单，并冻结其货物与舱室分配。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在")
    if plan.status != "success":
        raise HTTPException(
            status_code=409,
            detail=f"方案 {plan_id} 状态为 {plan.status}，仅 success 方案可创建执行单",
        )

    result = json.loads(plan.result_json)
    assignments = {a["cargo_id"]: a["compartment_id"] for a in result["assignments"]}

    execution = Execution(
        plan_id=plan.id,
        status=IN_PROGRESS,
        last_sequence=0,
        assignments_json=json.dumps(assignments, ensure_ascii=False),
    )
    db.add(execution)
    db.flush()  # 先取得 execution.id，再写入货物行
    for cargo_id in sorted(assignments, key=natural_key):
        db.add(ExecutionCargo(
            execution_id=execution.id,
            cargo_id=cargo_id,
            planned_compartment_id=assignments[cargo_id],
            status=PENDING,
        ))
    db.commit()
    db.refresh(execution)
    return serialize_execution(db, execution)


def get_execution(db: Session, execution_id: int) -> ExecutionOut:
    """按编号查询执行单当前状态。"""
    return serialize_execution(db, _get_execution(db, execution_id))


def _same_payload(row: ExecutionEvent, event: ScanEventIn) -> bool:
    """已存事件与新提交事件的载荷是否完全一致。"""
    return (
        row.sequence == event.sequence
        and row.cargo_id == event.cargo_id
        and row.action == event.action
        and row.compartment_id == event.compartment_id
    )


def _fail(db: Session, status_code: int, detail) -> None:
    """回滚当前事务并以指定错误中断批次处理。"""
    db.rollback()
    raise HTTPException(status_code=status_code, detail=detail)


def apply_event_batch(db: Session, execution_id: int, batch: EventBatchIn) -> EventBatchResult:
    """在同一事务中按序应用一批扫描事件。

    - 已存在的 event_id：载荷一致视为重试直接跳过，不一致返回 409；
    - 新事件：序号必须接续 last_sequence 连续递增，并逐条通过业务校验；
    - 任一新事件违规：整批回滚，返回 422、首个错误与提交前状态。
    """
    with _events_lock:
        execution = _get_execution(db, execution_id)
        # 提交前状态快照：用于违规时的错误响应，整批回滚后状态与此一致
        pre_cargos = _cargo_states(db, execution_id)
        pre_last_sequence = execution.last_sequence
        pre_status = execution.status

        known = {
            e.event_id: e
            for e in db.query(ExecutionEvent)
            .filter(ExecutionEvent.execution_id == execution_id)
            .all()
        }
        # 在内存副本上模拟状态迁移，全部通过后才落库
        sim = {c.cargo_id: c.status for c in pre_cargos}
        planned = {c.cargo_id: c.planned_compartment_id for c in pre_cargos}
        expected = pre_last_sequence + 1
        new_events: list[ScanEventIn] = []
        skipped = 0
        unloading_started = UNLOADED in sim.values()

        for event in batch.events:
            existing = known.get(event.event_id)
            if existing is not None:
                if _same_payload(existing, event):
                    skipped += 1  # 相同 event_id + 相同载荷：幂等重试，不重复推进
                    continue
                _fail(db, 409, {
                    "error": f"事件 {event.event_id} 已存在但载荷不一致",
                    "event_id": event.event_id,
                    "recorded": {
                        "sequence": existing.sequence,
                        "cargo_id": existing.cargo_id,
                        "action": existing.action,
                        "compartment_id": existing.compartment_id,
                    },
                })

            def violation(message: str) -> None:
                """整批回滚并返回首个错误与提交前状态。"""
                _fail(db, 422, {
                    "error": message,
                    "event_id": event.event_id,
                    "sequence": event.sequence,
                    "state": {
                        "execution_id": execution_id,
                        "status": pre_status,
                        "last_sequence": pre_last_sequence,
                        "cargos": [c.model_dump() for c in pre_cargos],
                    },
                })

            if event.sequence != expected:
                violation(f"序号不连续：期望 {expected}，收到 {event.sequence}")
            if event.cargo_id not in planned:
                violation(f"货物 {event.cargo_id} 不在执行单中")
            if event.compartment_id != planned[event.cargo_id]:
                violation(
                    f"货物 {event.cargo_id} 必须装入计划舱室 "
                    f"{planned[event.cargo_id]}，扫描到的是 {event.compartment_id}"
                )

            status = sim[event.cargo_id]
            if event.action == "load":
                if unloading_started:
                    violation("已开始卸载，不能再装载")
                if status != PENDING:
                    violation(f"货物 {event.cargo_id} 不得重复装载")
                sim[event.cargo_id] = LOADED
            else:  # unload
                if not unloading_started and any(s != LOADED for s in sim.values()):
                    violation("尚有货物未装载，不能开始卸载")
                if status == PENDING:
                    violation(f"货物 {event.cargo_id} 未装先卸")
                if status == UNLOADED:
                    violation(f"货物 {event.cargo_id} 不得重复卸载")
                sim[event.cargo_id] = UNLOADED
                unloading_started = True

            known[event.event_id] = event  # 供同批次后续事件做幂等比对
            new_events.append(event)
            expected += 1

        # 全部校验通过：在同一事务中按序落库
        for event in new_events:
            db.add(ExecutionEvent(
                execution_id=execution_id,
                event_id=event.event_id,
                sequence=event.sequence,
                cargo_id=event.cargo_id,
                action=event.action,
                compartment_id=event.compartment_id,
            ))
        if new_events:
            rows = (
                db.query(ExecutionCargo)
                .filter(ExecutionCargo.execution_id == execution_id)
                .all()
            )
            for row in rows:
                row.status = sim[row.cargo_id]
            execution.last_sequence = new_events[-1].sequence
            if all(s == UNLOADED for s in sim.values()):
                execution.status = COMPLETED  # 全部卸完自动完成
            execution.updated_at = _utcnow()
        db.commit()

        return EventBatchResult(
            execution_id=execution_id,
            applied=len(new_events),
            skipped=skipped,
            status=execution.status,
            last_sequence=execution.last_sequence,
            cargos=_cargo_states(db, execution_id),
        )

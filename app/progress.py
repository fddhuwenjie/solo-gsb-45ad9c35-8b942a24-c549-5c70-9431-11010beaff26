"""运输进度：success 路线与同方案执行单的绑定、checkpoint 批次应用与延误重排。

规则概述：
- 仅 status=success 的路线可绑定，且执行单须为**同方案**的 in_progress 单；
  不同方案、非 success 路线或已完成执行单不得绑定（409）；
  路线与执行单均只能绑定一次，绑定即创建可查询的运输进度；
- checkpoint 按 sequence 从 1 连续递增，新批次在同一事务中按序应用；
  segment_id 必须匹配当前待行路段（剩余计划首段），arrived_at 不倒退，
  且不早于该段物理到达时刻（上一到达时刻 + 该段行驶分钟）；
- 相同 checkpoint_id 且载荷相同的重试不重复推进；载荷不同返回 409；
- 任一新 checkpoint 违规：整批回滚，返回 422、首个错误与提交前状态；
- 实际到达晚于计划时，依据路线保存的道路图、禁运类别、禁行窗口与节点
  等待规则，从当前节点重排剩余路段，更新剩余计划与延误；重排无解则进度
  置为 blocked 且整批不提交；抵达终点后状态为 arrived。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Execution, Route, TransportCheckpoint, TransportProgress
from .route_service import _build_graph, _minute_origin, _serialize, _to_utc
from .routing import plan_route
from .schemas import (
    CheckpointBatchIn,
    CheckpointBatchResult,
    CheckpointIn,
    CheckpointOut,
    CompletedSegmentOut,
    ProgressCreateIn,
    ProgressOut,
    RouteRequest,
    RouteStepOut,
)

# 运输进度状态
IN_PROGRESS, ARRIVED, BLOCKED = "in_progress", "arrived", "blocked"

# 串行化 checkpoint 批次写入，避免并发批次交错推进同一进度
_checkpoints_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _get_progress(db: Session, progress_id: int) -> TransportProgress:
    progress = db.get(TransportProgress, progress_id)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"运输进度 {progress_id} 不存在")
    return progress


def _checkpoint_rows(db: Session, progress_id: int) -> list[TransportCheckpoint]:
    return (
        db.query(TransportCheckpoint)
        .filter(TransportCheckpoint.progress_id == progress_id)
        .order_by(TransportCheckpoint.sequence)
        .all()
    )


def serialize_progress(db: Session, progress: TransportProgress) -> ProgressOut:
    """进度完整视图：状态、已完成段、当前剩余计划、延误与 checkpoint 日志。"""
    route_result = json.loads(db.get(Route, progress.route_id).result_json)
    remaining = json.loads(progress.remaining_json)
    steps = remaining["steps"]
    rows = _checkpoint_rows(db, progress.id)
    return ProgressOut(
        progress_id=progress.id,
        route_id=progress.route_id,
        execution_id=progress.execution_id,
        plan_id=progress.plan_id,
        status=progress.status,
        last_sequence=progress.last_sequence,
        planned_arrival_at=route_result["arrival_at"],
        expected_arrival_at=remaining["expected_arrival_at"],
        delay_minutes=progress.delay_minutes,
        current_node=steps[0]["from_node"] if steps else route_result["destination"],
        completed_segments=[
            CompletedSegmentOut(
                sequence=r.sequence,
                checkpoint_id=r.checkpoint_id,
                segment_id=r.segment_id,
                arrived_at=r.arrived_at,
            )
            for r in rows
        ],
        remaining_plan=[RouteStepOut(**s) for s in steps],
        checkpoints=[
            CheckpointOut(
                checkpoint_id=r.checkpoint_id,
                sequence=r.sequence,
                segment_id=r.segment_id,
                arrived_at=r.arrived_at,
            )
            for r in rows
        ],
        blockages=json.loads(progress.blockages_json),
        created_at=progress.created_at,
        updated_at=progress.updated_at,
    )


def create_progress(db: Session, route_id: int, req: ProgressCreateIn) -> ProgressOut:
    """把 success 路线绑定到同方案的 in_progress 执行单，创建运输进度。"""
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail=f"路线 {route_id} 不存在")
    execution = db.get(Execution, req.execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"执行单 {req.execution_id} 不存在")
    if route.status != "success":
        raise HTTPException(
            status_code=409,
            detail=f"路线 {route_id} 状态为 {route.status}，仅 success 路线可绑定运输进度",
        )
    if execution.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail=f"执行单 {execution.id} 状态为 {execution.status}，仅 in_progress 执行单可绑定",
        )
    if route.plan_id != execution.plan_id:
        raise HTTPException(
            status_code=409,
            detail=f"路线 {route_id} 属于方案 {route.plan_id}，执行单 {execution.id} "
                   f"属于方案 {execution.plan_id}，不同方案不得绑定",
        )
    clash = (
        db.query(TransportProgress)
        .filter(
            (TransportProgress.route_id == route_id)
            | (TransportProgress.execution_id == execution.id)
        )
        .first()
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"路线 {clash.route_id} 或执行单 {clash.execution_id} "
                   f"已绑定运输进度 {clash.id}，不得重复绑定",
        )

    route_result = json.loads(route.result_json)
    remaining = {
        "steps": route_result["steps"],
        "expected_arrival_at": route_result["arrival_at"],
    }
    progress = TransportProgress(
        route_id=route.id,
        execution_id=execution.id,
        plan_id=route.plan_id,
        status=IN_PROGRESS,
        last_sequence=0,
        remaining_json=json.dumps(remaining, ensure_ascii=False),
        delay_minutes=0,
        blockages_json="[]",
    )
    db.add(progress)
    db.commit()
    db.refresh(progress)
    return serialize_progress(db, progress)


def get_progress(db: Session, progress_id: int) -> ProgressOut:
    """按编号查询运输进度。"""
    return serialize_progress(db, _get_progress(db, progress_id))


def _replan_remaining(route: Route, current_node: str, at: datetime) -> dict:
    """从当前节点以当前时刻重排剩余路段（道路图与载货类别取自路线快照）。"""
    route_result = json.loads(route.result_json)
    route_req = RouteRequest(**json.loads(route.request_json))
    nodes, segments = _build_graph(route_req, _minute_origin(at))  # at 已对齐整分钟
    result = plan_route(
        segments=segments,
        nodes=nodes,
        origin=current_node,
        destination=route_result["destination"],
        carried_classes=set(route_result["hazard_classes"]),
    )
    return _serialize(result, at, route_result["hazard_classes"])


def _iso_steps(steps: list[dict]) -> list[dict]:
    """把重排结果中的 datetime 转为 ISO 字符串以便落库。"""
    return [
        {
            **s,
            "departure_at": s["departure_at"].isoformat(),
            "arrival_at": s["arrival_at"].isoformat(),
        }
        for s in steps
    ]


def _iso_blockages(blockages: list[dict]) -> list[dict]:
    out = []
    for b in blockages:
        item = dict(b)
        if item.get("arrival_at") is not None:
            item["arrival_at"] = item["arrival_at"].isoformat()
        for w in item.get("windows", []):
            w["start"] = w["start"].isoformat()
            w["end"] = w["end"].isoformat()
        out.append(item)
    return out


def _same_payload(row, cp: CheckpointIn, arrived_iso: str) -> bool:
    """已存 checkpoint 与新提交 checkpoint 的载荷是否完全一致。"""
    return (
        row.sequence == cp.sequence
        and row.segment_id == cp.segment_id
        and row.arrived_at == arrived_iso
    )


def apply_checkpoint_batch(
    db: Session, progress_id: int, batch: CheckpointBatchIn
) -> CheckpointBatchResult:
    """在同一事务中按序应用一批 checkpoint。

    - 已存在的 checkpoint_id：载荷一致视为重试直接跳过，不一致返回 409；
    - 新 checkpoint：序号连续、匹配当前待行路段、时刻不倒退且不早于物理到达；
    - 实际到达晚于计划时从当前节点重排剩余路段；重排无解则进度置 blocked，
      整批不提交（仅状态与阻断原因落库）；
    - 任一新 checkpoint 违规：整批回滚，返回 422、首个错误与提交前状态。
    """
    with _checkpoints_lock:
        progress = _get_progress(db, progress_id)
        if progress.status == ARRIVED:
            raise HTTPException(
                status_code=409, detail=f"运输进度 {progress_id} 已抵达终点，不再接收上报"
            )
        if progress.status == BLOCKED:
            raise HTTPException(
                status_code=409, detail=f"运输进度 {progress_id} 已阻塞（重排无解），不再接收上报"
            )

        route = db.get(Route, progress.route_id)
        route_result = json.loads(route.result_json)
        planned_arrival = _parse(route_result["arrival_at"])

        # 提交前状态快照：用于违规时的错误响应，整批回滚后状态与此一致
        pre = serialize_progress(db, progress)

        known = {c.checkpoint_id: c for c in _checkpoint_rows(db, progress_id)}
        last = (
            db.query(TransportCheckpoint)
            .filter(TransportCheckpoint.progress_id == progress_id)
            .order_by(TransportCheckpoint.sequence.desc())
            .first()
        )
        # 首条 checkpoint 的上一到达时刻即路线出发时刻
        prev_arrived = _parse(last.arrived_at) if last else _parse(route_result["departure_at"])

        remaining = json.loads(progress.remaining_json)
        # 在内存副本上模拟状态迁移，全部通过后才落库
        sim_steps = list(remaining["steps"])
        sim_expected = remaining["expected_arrival_at"]
        sim_delay = progress.delay_minutes
        sim_status = progress.status
        expected_seq = progress.last_sequence + 1
        new_items: list[tuple[CheckpointIn, str]] = []
        skipped = 0

        for cp in batch.checkpoints:
            arrived = _to_utc(cp.arrived_at)
            arrived_iso = arrived.isoformat()
            existing = known.get(cp.checkpoint_id)
            if existing is not None:
                if _same_payload(existing, cp, arrived_iso):
                    skipped += 1  # 相同 checkpoint_id + 相同载荷：幂等重试，不重复推进
                    continue
                db.rollback()
                raise HTTPException(status_code=409, detail={
                    "error": f"checkpoint {cp.checkpoint_id} 已存在但载荷不一致",
                    "checkpoint_id": cp.checkpoint_id,
                    "recorded": {
                        "sequence": existing.sequence,
                        "segment_id": existing.segment_id,
                        "arrived_at": existing.arrived_at,
                    },
                })

            def violation(message: str) -> None:
                """整批回滚并返回首个错误与提交前状态。"""
                db.rollback()
                raise HTTPException(status_code=422, detail={
                    "error": message,
                    "checkpoint_id": cp.checkpoint_id,
                    "sequence": cp.sequence,
                    "state": pre.model_dump(mode="json"),
                })

            if cp.sequence != expected_seq:
                violation(f"序号不连续：期望 {expected_seq}，收到 {cp.sequence}")
            if arrived.second != 0 or arrived.microsecond != 0:
                violation(f"到达时刻 {arrived_iso} 必须对齐到整分钟（秒与微秒为 0）")
            if not sim_steps:
                violation("已抵达终点，无待行路段")
            step = sim_steps[0]
            if cp.segment_id != step["segment_id"]:
                violation(f"当前待行路段为 {step['segment_id']}，上报的是 {cp.segment_id}")
            if arrived < prev_arrived:
                violation(
                    f"到达时刻 {arrived_iso} 早于上一到达时刻 "
                    f"{prev_arrived.isoformat()}，时间不得倒退"
                )
            earliest = prev_arrived + timedelta(minutes=step["drive_minutes"])
            if arrived < earliest:
                violation(
                    f"到达时刻 {arrived_iso} 早于该段物理到达时刻 {earliest.isoformat()}"
                    f"（上一到达时刻 + 行驶 {step['drive_minutes']} 分钟）"
                )

            # 应用：消费当前待行路段
            sim_steps.pop(0)
            prev_arrived = arrived
            if not sim_steps:
                # 抵达终点：无需重排，延误按实际到达结算
                sim_status = ARRIVED
                sim_expected = arrived_iso
                sim_delay = max(0, int((arrived - planned_arrival).total_seconds() // 60))
            elif arrived > _parse(step["arrival_at"]):
                # 晚于计划：从当前节点（该段终点）按路线保存的道路图重排剩余路段
                payload = _replan_remaining(route, step["to_node"], arrived)
                if payload["status"] != "success":
                    # 重排无解：进度置 blocked 并持久化，整批不提交
                    progress.status = BLOCKED
                    progress.blockages_json = json.dumps(
                        _iso_blockages(payload["blockages"]), ensure_ascii=False
                    )
                    progress.updated_at = _utcnow()
                    db.commit()
                    view = serialize_progress(db, progress)
                    return CheckpointBatchResult(
                        progress_id=progress.id,
                        applied=0,
                        skipped=skipped,
                        status=view.status,
                        last_sequence=view.last_sequence,
                        delay_minutes=view.delay_minutes,
                        expected_arrival_at=view.expected_arrival_at,
                        completed_segments=view.completed_segments,
                        remaining_plan=view.remaining_plan,
                        blockages=view.blockages,
                    )
                sim_steps = _iso_steps(payload["steps"])
                sim_expected = payload["arrival_at"].isoformat()
                sim_delay = max(
                    0, int((payload["arrival_at"] - planned_arrival).total_seconds() // 60)
                )

            # 供同批次后续 checkpoint 做幂等比对（arrived_at 已规范化为 UTC ISO）
            known[cp.checkpoint_id] = SimpleNamespace(
                sequence=cp.sequence, segment_id=cp.segment_id, arrived_at=arrived_iso
            )
            new_items.append((cp, arrived_iso))
            expected_seq += 1

        # 全部校验通过：在同一事务中按序落库
        for cp, arrived_iso in new_items:
            db.add(TransportCheckpoint(
                progress_id=progress_id,
                checkpoint_id=cp.checkpoint_id,
                sequence=cp.sequence,
                segment_id=cp.segment_id,
                arrived_at=arrived_iso,
            ))
        if new_items:
            progress.last_sequence = new_items[-1][0].sequence
            progress.status = sim_status
            progress.remaining_json = json.dumps(
                {"steps": sim_steps, "expected_arrival_at": sim_expected},
                ensure_ascii=False,
            )
            progress.delay_minutes = sim_delay
            progress.updated_at = _utcnow()
        db.commit()

        view = serialize_progress(db, progress)
        return CheckpointBatchResult(
            progress_id=progress.id,
            applied=len(new_items),
            skipped=skipped,
            status=view.status,
            last_sequence=view.last_sequence,
            delay_minutes=view.delay_minutes,
            expected_arrival_at=view.expected_arrival_at,
            completed_segments=view.completed_segments,
            remaining_plan=view.remaining_plan,
            blockages=view.blockages,
        )

"""危险品运输进度：路线绑定执行单、checkpoint 批次上报、延误重排。

规则概述：
- 仅 status=success 的路线可绑定，执行单必须 status=in_progress 且与路线
  属于同一方案；同一执行单至多绑定一条路线（重复绑定 409）；
- checkpoint 携带唯一 checkpoint_id、连续 sequence、segment_id、arrived_at；
  segment 必须是当前待行路段，到达时刻不倒退、不得早于该段物理到达时刻，
  且必须带时区并对齐整分钟；
- 相同 checkpoint_id + 相同载荷的重试幂等跳过；相同编号 + 不同载荷返回 409；
- 一批中任一错误整批回滚（422），返回首个错误与提交前状态；
- 实际到达晚于该段计划到达时，依据路线保存的道路图、禁运类别、禁行窗口与
  节点等待规则，从当前节点、以实际到达时刻为起点重排剩余路段；
- 重排有解：提交该批并返回已完成段、调整后的剩余计划与延误；
- 重排无解：进度置为 blocked、阻断原因持久化（进度仍可查询），但该批
  checkpoint 不提交；抵达终点后置为 arrived。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import (
    Execution,
    Plan,
    Route,
    TransportCheckpoint,
    TransportProgress,
)
from .route_service import _jsonable, plan_from_graph
from .schemas import (
    CheckpointBatchIn,
    CheckpointBatchResult,
    CheckpointIn,
    ProgressBindIn,
    RouteRequest,
    TransportProgressOut,
)

UTC = timezone.utc

# 进度状态
IN_TRANSIT, BLOCKED, ARRIVED = "in_transit", "blocked", "arrived"

# 串行化同一进度的批次写入，避免并发批次交错推进（与事件批次一致）
_progress_lock = threading.Lock()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _iso(v: datetime) -> str:
    return _utc(v).isoformat()


def _utc(v: datetime) -> datetime:
    return v.astimezone(UTC)


def _get_progress(db: Session, progress_id: int) -> TransportProgress:
    progress = db.get(TransportProgress, progress_id)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"运输进度 {progress_id} 不存在")
    return progress


def bind_route(
    db: Session, execution_id: int, body: ProgressBindIn
) -> TransportProgressOut:
    """把一条 success 路线绑定到同方案的 in_progress 执行单，创建运输进度。"""
    execution = db.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"执行单 {execution_id} 不存在")
    if execution.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail=f"执行单 {execution_id} 状态为 {execution.status}，"
            f"仅 in_progress 执行单可绑定运输路线",
        )

    existing = (
        db.query(TransportProgress)
        .filter(TransportProgress.execution_id == execution_id)
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"执行单 {execution_id} 已绑定路线 {existing.route_id}（进度 {existing.id}）",
        )

    route = db.get(Route, body.route_id)
    if route is None:
        raise HTTPException(status_code=404, detail=f"路线 {body.route_id} 不存在")
    if route.status != "success":
        raise HTTPException(
            status_code=409,
            detail=f"路线 {body.route_id} 状态为 {route.status}，仅 success 路线可绑定",
        )
    plan = db.get(Plan, route.plan_id)
    if plan is None or plan.status != "success":
        # 路线绑定方案：success 路线必然来自 success 方案，此处为数据一致性兜底
        raise HTTPException(
            status_code=409,
            detail=f"路线 {body.route_id} 所属方案 {route.plan_id} 非 success，不得绑定",
        )
    if route.plan_id != execution.plan_id:
        raise HTTPException(
            status_code=409,
            detail=f"路线 {body.route_id} 属于方案 {route.plan_id}，"
            f"执行单 {execution_id} 属于方案 {execution.plan_id}，方案不同不得绑定",
        )

    result = json.loads(route.result_json)
    # 初始剩余计划即路线编排结果的完整逐段计划
    remaining_steps = result["steps"]
    remaining_waits = result["waits"]
    state = {
        "remaining_steps": remaining_steps,
        "remaining_waits": remaining_waits,
        "planned_arrival_at": result["arrival_at"],
        "delay_minutes": 0,
        "blockages": [],
    }
    plan_snapshot = {
        "origin": result["origin"],
        "destination": result["destination"],
        "departure_at": result["departure_at"],
        "original_arrival_at": result["arrival_at"],
        "hazard_classes": result["hazard_classes"],
    }

    progress = TransportProgress(
        execution_id=execution_id,
        route_id=route.id,
        plan_id=route.plan_id,
        status=IN_TRANSIT,
        current_node=result["origin"],
        last_sequence=0,
        plan_json=json.dumps(plan_snapshot, ensure_ascii=False),
        state_json=json.dumps(state, ensure_ascii=False),
    )
    db.add(progress)
    db.commit()
    db.refresh(progress)
    return serialize_progress(db, progress)


def _load_checkpoints(db: Session, progress_id: int) -> list[TransportCheckpoint]:
    return (
        db.query(TransportCheckpoint)
        .filter(TransportCheckpoint.progress_id == progress_id)
        .order_by(TransportCheckpoint.sequence)
        .all()
    )


def serialize_progress(db: Session, progress: TransportProgress) -> TransportProgressOut:
    """进度完整视图：绑定信息、已完成段、调整后的剩余计划与延误。"""
    plan = json.loads(progress.plan_json)
    state = json.loads(progress.state_json)
    completed = [
        {
            "checkpoint_id": c.checkpoint_id,
            "sequence": c.sequence,
            "segment_id": c.segment_id,
            "from_node": c.from_node,
            "to_node": c.to_node,
            "planned_departure_at": c.planned_departure_at,
            "planned_arrival_at": c.planned_arrival_at,
            "arrived_at": c.arrived_at,
            "drive_minutes": c.drive_minutes,
            "delay_minutes": c.delay_minutes,
        }
        for c in _load_checkpoints(db, progress.id)
    ]
    remaining_steps = state.get("remaining_steps", [])
    return TransportProgressOut(
        progress_id=progress.id,
        execution_id=progress.execution_id,
        route_id=progress.route_id,
        plan_id=progress.plan_id,
        status=progress.status,
        origin=plan["origin"],
        destination=plan["destination"],
        departure_at=plan["departure_at"],
        original_arrival_at=plan["original_arrival_at"],
        current_node=progress.current_node,
        planned_arrival_at=state.get("planned_arrival_at"),
        delay_minutes=state.get("delay_minutes", 0),
        hazard_classes=plan["hazard_classes"],
        last_sequence=progress.last_sequence,
        completed=completed,
        remaining_segment_ids=[s["segment_id"] for s in remaining_steps],
        remaining_steps=remaining_steps,
        remaining_waits=state.get("remaining_waits", []),
        blockages=state.get("blockages", []),
        created_at=progress.created_at,
        updated_at=progress.updated_at,
    )


def get_progress(db: Session, progress_id: int) -> TransportProgressOut:
    """按编号查询运输进度。"""
    return serialize_progress(db, _get_progress(db, progress_id))


def apply_checkpoint_batch(
    db: Session, progress_id: int, batch: CheckpointBatchIn
) -> CheckpointBatchResult:
    """在同一事务中按序应用一批 checkpoint。

    - 已存在的 checkpoint_id：载荷一致视为重试跳过，不一致返回 409；
    - 新 checkpoint：序号连续、segment 为当前待行路段、时间不倒退且不早于
      该段物理到达时刻、整分钟对齐；
    - 延误（实际晚于计划）则从当前节点重排剩余路段；重排无解置 blocked，
      阻断原因持久化但该批 checkpoint 不提交；
    - 任一新 checkpoint 违规：整批回滚，返回 422、首个错误与提交前状态。
    """
    with _progress_lock:
        progress = _get_progress(db, progress_id)
        route = db.get(Route, progress.route_id)
        # 提交前状态快照：违规错误响应中随附，整批回滚后状态与此一致
        pre_view = serialize_progress(db, progress).model_dump(mode="json")

        known = {
            c.checkpoint_id: {
                "sequence": c.sequence,
                "segment_id": c.segment_id,
                "arrived_at": c.arrived_at,
            }
            for c in _load_checkpoints(db, progress_id)
        }
        # 已持久化的最后到达时刻（跨批次的时间不倒退/物理到达下界）
        persisted_rows = (
            db.query(TransportCheckpoint)
            .filter(TransportCheckpoint.progress_id == progress_id)
            .order_by(TransportCheckpoint.sequence.desc())
            .first()
        )
        last_persisted_arrival = (
            _parse_dt(persisted_rows.arrived_at) if persisted_rows is not None else None
        )
        # 提交前动态状态：重排无解时整批不提交，须回滚到这份初始状态
        state_before_batch = json.loads(progress.state_json)
        node_before_batch = progress.current_node
        sim_state = json.loads(json.dumps(state_before_batch))
        current_node = progress.current_node
        expected_seq = progress.last_sequence + 1
        new_rows: list[dict] = []
        skipped = 0

        def fail(status_code: int, detail: dict) -> None:
            """回滚当前事务并以指定错误中断批次处理。"""
            db.rollback()
            raise HTTPException(status_code=status_code, detail=detail)

        def violation(cp: CheckpointIn | None, message: str,
                      **extra) -> None:
            """整批回滚并返回首个错误与提交前状态。"""
            detail = {
                "error": message,
                "state": pre_view,
            }
            if cp is not None:
                detail["checkpoint_id"] = cp.checkpoint_id
                detail["sequence"] = cp.sequence
            detail.update(extra)
            fail(422, detail)

        for cp in batch.checkpoints:
            existing = known.get(cp.checkpoint_id)
            if existing is not None:
                same = (
                    existing["sequence"] == cp.sequence
                    and existing["segment_id"] == cp.segment_id
                    and existing["arrived_at"] == _iso(cp.arrived_at)
                )
                if same:
                    skipped += 1  # 相同 checkpoint_id + 相同载荷：幂等重试（任意状态下均允许）
                    continue
                fail(409, {
                    "error": f"checkpoint {cp.checkpoint_id} 已存在但载荷不一致",
                    "checkpoint_id": cp.checkpoint_id,
                    "recorded": existing,
                })

            # 新 checkpoint 仅在运输中可推进；blocked/arrived 一律拒绝（整批回滚）
            if progress.status != IN_TRANSIT:
                violation(
                    cp,
                    f"进度 {progress_id} 状态为 {progress.status}，仅 in_transit 进度可上报新 checkpoint",
                )

            remaining_steps = sim_state["remaining_steps"]
            remaining_waits = sim_state.get("remaining_waits", [])
            arrived = _utc(cp.arrived_at)
            if arrived.second != 0 or arrived.microsecond != 0:
                violation(cp, f"到达时刻 {arrived.isoformat()} 必须对齐到整分钟（秒与微秒为 0）")
            if cp.sequence != expected_seq:
                violation(cp, f"序号不连续：期望 {expected_seq}，收到 {cp.sequence}")
            if not remaining_steps:
                violation(cp, "剩余计划已无待行路段（车辆已抵达终点），不得再上报 checkpoint")
            step = remaining_steps[0]
            if cp.segment_id != step["segment_id"]:
                violation(
                    cp,
                    f"checkpoint 路段 {cp.segment_id} 不是当前待行路段 {step['segment_id']}",
                )
            if step["from_node"] != current_node:
                # 数据一致性兜底：剩余计划首段必须从当前节点出发
                violation(
                    cp,
                    f"路段 {step['segment_id']} 起点 {step['from_node']} 与当前节点 "
                    f"{current_node} 不一致",
                )
            # 时间不倒退：不得早于上一条 checkpoint 的实际到达时刻（首段不得早于出发）
            if new_rows:
                prev_arrived = _parse_dt(new_rows[-1]["arrived_at"])
            elif last_persisted_arrival is not None:
                prev_arrived = last_persisted_arrival  # 新批次：上一条已持久化
            else:
                prev_arrived = _parse_dt(json.loads(progress.plan_json)["departure_at"])
            if arrived < prev_arrived:
                violation(
                    cp,
                    f"到达时刻 {arrived.isoformat()} 早于上一到达时刻 {_iso(prev_arrived)}，时间不得倒退",
                )
            # 物理到达时刻：当前节点即上一条 checkpoint 的 to_node（首段为起点），
            # 从进入该节点的实际时刻起，最快经过该段全程行驶分钟后才能到达
            node_arrival = prev_arrived
            physical = node_arrival + timedelta(minutes=step["drive_minutes"])
            if arrived < physical:
                violation(
                    cp,
                    f"到达时刻 {arrived.isoformat()} 早于该段物理最早到达时刻 "
                    f"{_iso(physical)}（{step['from_node']} 于 {_iso(node_arrival)} 到达，"
                    f"行驶 {step['drive_minutes']} 分钟）",
                )

            planned_departure = _parse_dt(step["departure_at"])
            planned_arrival = _parse_dt(step["arrival_at"])
            seg_delay = int((arrived - planned_arrival).total_seconds() // 60)
            plan_snapshot = json.loads(progress.plan_json)

            completed_row = {
                "checkpoint_id": cp.checkpoint_id,
                "sequence": cp.sequence,
                "segment_id": step["segment_id"],
                "from_node": step["from_node"],
                "to_node": step["to_node"],
                "planned_departure_at": _iso(planned_departure),
                "planned_arrival_at": _iso(planned_arrival),
                "arrived_at": _iso(arrived),
                "drive_minutes": step["drive_minutes"],
                "delay_minutes": seg_delay,
            }
            current_node = step["to_node"]
            tail_steps = remaining_steps[1:]
            # 消费首段计划时同步弹出其等待记录（等待挂在该段的上游节点）
            tail_waits = remaining_waits
            if tail_waits and tail_waits[0].get("for_segment_id") == step["segment_id"]:
                tail_waits = tail_waits[1:]

            if not tail_steps:
                # 已抵达终点：不再重排；总延误为实际到达相对原始计划到达
                sim_state = {
                    "remaining_steps": [],
                    "remaining_waits": [],
                    "planned_arrival_at": _iso(arrived),
                    "delay_minutes": int(
                        (arrived - _parse_dt(
                            plan_snapshot["original_arrival_at"]
                        )).total_seconds() // 60
                    ),
                    "blockages": [],
                }
            elif seg_delay > 0:
                # 实际到达晚于计划：从当前节点、以实际到达时刻为零点重排剩余路段
                req = RouteRequest.model_validate(json.loads(route.request_json))
                replanned = plan_from_graph(
                    req,
                    arrived,
                    current_node,
                    plan_snapshot["destination"],
                    plan_snapshot["hazard_classes"],
                )
                rep_jsonable = _jsonable(replanned)
                if replanned["status"] != "success":
                    # 重排无解：进度置 blocked 并持久化阻断原因，但该批不提交。
                    # 该批 checkpoint 不落库，故车辆位置与剩余计划维持提交前状态
                    # （本批已消费的路段同样回滚），仅记录最近一次延误供查询。
                    blocked_state = dict(state_before_batch)
                    blocked_state["planned_arrival_at"] = None
                    blocked_state["delay_minutes"] = seg_delay
                    blocked_state["blockages"] = rep_jsonable["blockages"]
                    progress.status = BLOCKED
                    progress.current_node = node_before_batch
                    progress.state_json = json.dumps(blocked_state, ensure_ascii=False)
                    progress.updated_at = datetime.now(UTC)
                    db.commit()
                    blocked_view = serialize_progress(db, progress).model_dump(mode="json")
                    db.rollback()
                    raise HTTPException(status_code=422, detail={
                        "error": f"车辆延误至 {_iso(arrived)}，从节点 {current_node} "
                                 f"重排剩余路段无可行方案，进度已置为 blocked，该批 checkpoint 不提交",
                        "checkpoint_id": cp.checkpoint_id,
                        "sequence": cp.sequence,
                        "blockages": blocked_view["blockages"],
                        "state": blocked_view,
                    })
                sim_state = {
                    "remaining_steps": rep_jsonable["steps"],
                    "remaining_waits": rep_jsonable["waits"],
                    "planned_arrival_at": rep_jsonable["arrival_at"],
                    "delay_minutes": int(
                        (_parse_dt(rep_jsonable["arrival_at"]) - _parse_dt(
                            plan_snapshot["original_arrival_at"]
                        )).total_seconds() // 60
                    ),
                    "blockages": [],
                }
            else:
                # 准点或提前：剩余计划维持不变
                sim_state = {
                    "remaining_steps": tail_steps,
                    "remaining_waits": tail_waits,
                    "planned_arrival_at": sim_state["planned_arrival_at"],
                    "delay_minutes": 0,
                    "blockages": [],
                }

            known[cp.checkpoint_id] = {
                "sequence": cp.sequence,
                "segment_id": cp.segment_id,
                "arrived_at": _iso(arrived),
            }  # 供同批次后续相同编号做幂等比对
            new_rows.append(completed_row)
            expected_seq += 1

        # 全部校验通过：在同一事务中按序落库
        for row in new_rows:
            db.add(TransportCheckpoint(
                progress_id=progress_id,
                **row,
            ))
        if new_rows:
            progress.current_node = current_node
            progress.last_sequence = new_rows[-1]["sequence"]
            progress.state_json = json.dumps(sim_state, ensure_ascii=False)
            if not sim_state["remaining_steps"]:
                progress.status = ARRIVED  # 抵达终点
            progress.updated_at = datetime.now(UTC)
        db.commit()

        view = serialize_progress(db, progress)
        return CheckpointBatchResult(
            **view.model_dump(mode="json"),
            applied=len(new_rows),
            skipped=skipped,
        )

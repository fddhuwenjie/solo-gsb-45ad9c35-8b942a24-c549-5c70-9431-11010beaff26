"""危险品运输路线编排服务：危险类别提取、时间换算、编排与持久化。

- 危险类别从方案的**原始货物**（POST /plans 的请求快照）提取，为全部货物
  hazard_class 的去重集合；路段禁运类别与该集合相交即恒闭；
- 所有时刻先换算为相对出发时刻的整分钟偏移交给算法（禁行窗口必须与整分钟
  边界对齐，否则 422），输出再换算回 UTC 绝对时刻；
- 仅 status=success 的方案可编排路线（否则 409）；结果落库，支持按
  方案编号 / 路线编号查询。
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Plan, Route
from .routing import (
    RoadNode,
    RoadSegment,
    RoutePlan,
    Window,
    plan_route,
)
from .schemas import RouteRequest, RouteResponse, RouteSummary

UTC = timezone.utc


def _to_utc(v: datetime) -> datetime:
    return v.astimezone(UTC)


def _minute_origin(v: datetime) -> int:
    """校验为整分钟（UTC）并返回相对 Unix epoch 的分钟偏移。"""
    if v.second != 0 or v.microsecond != 0:
        raise HTTPException(
            status_code=422,
            detail=f"时刻 {v.isoformat()} 必须对齐到整分钟（秒与微秒为 0）",
        )
    return int(v.timestamp() // 60)


def _from_offsets(base: datetime, *offsets: int) -> tuple[datetime, ...]:
    return tuple(base + timedelta(minutes=o) for o in offsets)


def extract_hazard_classes(plan: Plan) -> list[str]:
    """从方案的原始货物请求中提取去重的危险类别（稳定排序）。"""
    request = json.loads(plan.request_json)
    return sorted({c["hazard_class"] for c in request["cargos"]})


def _build_graph(req: RouteRequest, departure_epoch_min: int):
    """把请求转换为算法输入（窗口换算为相对出发时刻的分钟偏移）。"""
    nodes = [RoadNode(n.node_id, n.allow_waiting) for n in req.nodes]
    segments: list[RoadSegment] = []
    for s in req.segments:
        windows: list[Window] = []
        for w in s.windows:
            ws = _minute_origin(_to_utc(w.start)) - departure_epoch_min
            we = _minute_origin(_to_utc(w.end)) - departure_epoch_min
            windows.append(Window(ws, we))
        windows.sort(key=lambda w: (w.start, w.end))
        segments.append(RoadSegment(
            segment_id=s.segment_id,
            from_node=s.from_node,
            to_node=s.to_node,
            drive_minutes=s.drive_minutes,
            embargo_classes=frozenset(s.embargo_classes),
            windows=tuple(windows),
        ))
    return nodes, segments


def _serialize(
    result: RoutePlan,
    departure,
    hazard_classes: list[str],
) -> dict:
    """把算法结果（分钟偏移）组装为响应字典。"""
    if result.status == "success":
        steps = []
        for st in result.steps:
            dep_dt, arr_dt = _from_offsets(departure, st.departure, st.arrival)
            steps.append({
                "segment_id": st.segment_id,
                "from_node": st.from_node,
                "to_node": st.to_node,
                "departure_at": dep_dt,
                "arrival_at": arr_dt,
                "drive_minutes": st.drive_minutes,
                "wait_before_minutes": st.wait_before_minutes,
            })
        waits = []
        for wt in result.waits:
            start_dt, end_dt = _from_offsets(departure, wt.start, wt.end)
            waits.append({
                "node_id": wt.node_id,
                "for_segment_id": wt.for_segment_id,
                "start_at": start_dt,
                "end_at": end_dt,
                "wait_minutes": wt.wait_minutes,
                "blocking_windows": [
                    {"start": departure + timedelta(minutes=w.start),
                     "end": departure + timedelta(minutes=w.end)}
                    for w in wt.blocking_windows
                ],
            })
        arrival = departure + timedelta(minutes=result.arrival) if result.arrival is not None else None
        return {
            "status": "success",
            "arrival_at": arrival,
            "total_drive_minutes": result.total_drive,
            "total_wait_minutes": result.total_wait,
            "segment_ids": [st.segment_id for st in result.steps],
            "steps": steps,
            "waits": waits,
            "blockages": [],
        }

    blockages = []
    for b in result.blockages:
        item: dict = {"type": b.type}
        if b.type == "embargo":
            item.update({
                "node_id": b.node_id,
                "segment_id": b.segment_id,
                "to_node": b.to_node,
                "embargo_classes": list(b.embargo_classes),
                "matched_classes": list(b.matched_classes),
            })
        elif b.type == "no_waiting":
            arrival_dt = departure + timedelta(minutes=b.arrival)
            item.update({
                "node_id": b.node_id,
                "segment_id": b.segment_id,
                "to_node": b.to_node,
                "arrival_at": arrival_dt,
                "drive_minutes": b.drive_minutes,
                "windows": [
                    {"start": departure + timedelta(minutes=w.start),
                     "end": departure + timedelta(minutes=w.end)}
                    for w in b.windows
                ],
            })
        else:  # unreachable
            item.update({"origin": b.origin, "destination": b.destination})
        blockages.append(item)
    return {
        "status": "infeasible",
        "arrival_at": None,
        "total_drive_minutes": 0,
        "total_wait_minutes": 0,
        "segment_ids": [],
        "steps": [],
        "waits": [],
        "blockages": blockages,
    }


def create_route(db: Session, plan_id: int, req: RouteRequest) -> RouteResponse:
    """为方案编排危险品运输路线并持久化。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在")
    if plan.status != "success":
        raise HTTPException(
            status_code=409,
            detail=f"方案 {plan_id} 状态为 {plan.status}，仅 success 方案可编排运输路线",
        )

    hazard_classes = extract_hazard_classes(plan)
    departure = _to_utc(req.departure_at)
    departure_epoch_min = _minute_origin(departure)
    nodes, segments = _build_graph(req, departure_epoch_min)

    result = plan_route(
        segments=segments,
        nodes=nodes,
        origin=req.origin,
        destination=req.destination,
        carried_classes=set(hazard_classes),
    )
    payload = _serialize(result, departure, hazard_classes)
    payload.update({
        "plan_id": plan_id,
        "origin": req.origin,
        "destination": req.destination,
        "departure_at": departure,
        "hazard_classes": hazard_classes,
    })

    route = Route(
        plan_id=plan_id,
        status=payload["status"],
        request_json=req.model_dump_json(),
        result_json=json.dumps(_jsonable(payload), ensure_ascii=False),
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return _to_response(route)


def _jsonable(payload: dict) -> dict:
    """把响应字典中的 datetime 转为 ISO 字符串以便 JSON 落库。"""
    out = dict(payload)
    out["departure_at"] = payload["departure_at"].isoformat()
    if payload.get("arrival_at") is not None:
        out["arrival_at"] = payload["arrival_at"].isoformat()
    for st in out["steps"]:
        st["departure_at"] = st["departure_at"].isoformat()
        st["arrival_at"] = st["arrival_at"].isoformat()
    for wt in out["waits"]:
        wt["start_at"] = wt["start_at"].isoformat()
        wt["end_at"] = wt["end_at"].isoformat()
        for w in wt["blocking_windows"]:
            w["start"] = w["start"].isoformat()
            w["end"] = w["end"].isoformat()
    for b in out["blockages"]:
        if b.get("arrival_at") is not None:
            b["arrival_at"] = b["arrival_at"].isoformat()
        for w in b.get("windows", []):
            w["start"] = w["start"].isoformat()
            w["end"] = w["end"].isoformat()
    return out


def _to_response(route: Route) -> RouteResponse:
    """从持久化记录重建响应。"""
    result = json.loads(route.result_json)
    result["route_id"] = route.id
    result["created_at"] = route.created_at
    return RouteResponse(**result)


def get_route(db: Session, route_id: int) -> RouteResponse:
    """按路线编号查询。"""
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail=f"路线 {route_id} 不存在")
    return _to_response(route)


def list_routes_for_plan(db: Session, plan_id: int) -> list[RouteSummary]:
    """列出某方案下的全部路线（先确认方案存在）。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在")
    rows = db.query(Route).filter(Route.plan_id == plan_id).order_by(Route.id).all()
    summaries: list[RouteSummary] = []
    for r in rows:
        result = json.loads(r.result_json)
        summaries.append(RouteSummary(
            route_id=r.id,
            plan_id=r.plan_id,
            status=r.status,
            origin=result["origin"],
            destination=result["destination"],
            departure_at=result["departure_at"],
            arrival_at=result.get("arrival_at"),
            total_wait_minutes=result.get("total_wait_minutes", 0),
            created_at=r.created_at,
        ))
    return summaries

"""路线编排服务测试：类别提取、UTC 换算、等待/阻断、持久化与错误码。"""
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import models  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import create_plan, create_plan_route, get_route_state, list_plan_routes  # noqa: E402
from app.route_service import create_route, extract_hazard_classes, get_route  # noqa: E402
from app.schemas import (  # noqa: E402
    PlanRequest,
    RestrictionWindowIn,
    RoadNodeIn,
    RouteRequest,
)
from app.seed import seed_rules  # noqa: E402

Base.metadata.create_all(bind=engine)
_seed_db = SessionLocal()
seed_rules(_seed_db)
_seed_db.close()


@contextmanager
def _raises(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"应抛出 {exc_type.__name__}")


def _db():
    return SessionLocal()


def _make_plan(db) -> int:
    """方案原始货物：G1 类别 3、G2 类别 5.1、G3 类别 3。"""
    req = PlanRequest(
        cargos=[
            {"cargo_id": "G1", "hazard_class": "3", "quantity": 10},
            {"cargo_id": "G2", "hazard_class": "5.1", "quantity": 10},
            {"cargo_id": "G3", "hazard_class": "3", "quantity": 5},
        ],
        compartments=[
            {"compartment_id": "1", "capacity": 20},
            {"compartment_id": "2", "capacity": 20},
        ],
    )
    resp = create_plan(req, db)
    assert resp.status == "success"
    return resp.plan_id


def _window(start, end):
    return RestrictionWindowIn(start=start, end=end)


def _graph_request(extra=None, **overrides):
    payload = {
        "nodes": [
            {"node_id": "A", "allow_waiting": True},
            {"node_id": "M", "allow_waiting": False},
            {"node_id": "Z", "allow_waiting": False},
        ],
        "segments": [
            {"segment_id": "r1", "from_node": "A", "to_node": "Z",
             "drive_minutes": 10, "embargo_classes": [], "windows": []},
            {"segment_id": "r2", "from_node": "A", "to_node": "M",
             "drive_minutes": 6, "embargo_classes": [], "windows": []},
            {"segment_id": "r3", "from_node": "M", "to_node": "Z",
             "drive_minutes": 4, "embargo_classes": [], "windows": []},
        ],
        "origin": "A",
        "destination": "Z",
        "departure_at": "2026-09-29T08:00:00Z",
    }
    if extra:
        payload.update(extra)
    payload.update(overrides)
    return RouteRequest(**payload)


def test_hazard_classes_extracted_from_original_cargos():
    db = _db()
    try:
        plan_id = _make_plan(db)
        plan = db.get(models.Plan, plan_id)
        assert extract_hazard_classes(plan) == ["3", "5.1"]
    finally:
        db.close()


def test_simple_route_persisted_and_queryable():
    db = _db()
    try:
        plan_id = _make_plan(db)
        resp = create_route(db, plan_id, _graph_request())
        assert resp.status == "success"
        assert resp.route_id is not None
        assert resp.plan_id == plan_id
        assert resp.hazard_classes == ["3", "5.1"]
        # 直达 r1 10 分钟最早
        assert resp.segment_ids == ["r1"]
        assert resp.arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert resp.total_drive_minutes == 10 and resp.total_wait_minutes == 0
        step = resp.steps[0]
        assert step.departure_at.isoformat() == "2026-09-29T08:00:00+00:00"
        assert step.arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert step.wait_before_minutes == 0
        # 按编号查询结果一致
        got = get_route(db, resp.route_id)
        assert got.segment_ids == ["r1"]
        assert got.arrival_at == resp.arrival_at
        # 端点函数同样可用
        assert get_route_state(resp.route_id, db).route_id == resp.route_id
        summaries = list_plan_routes(plan_id, db)
        assert [s.route_id for s in summaries] == [resp.route_id]
        assert summaries[0].status == "success"
    finally:
        db.close()


def test_detour_beats_waiting_when_arrival_earlier():
    db = _db()
    try:
        plan_id = _make_plan(db)
        # r1 窗口 [08:00,08:15) 半开；08:00 出发的通行区间 [08:00,08:10) 相交；
        # 绕行 r2+r3=10 分钟无窗口 => 08:10 到达，严格早于等待后的 08:25
        req = _graph_request()
        req.segments[0].windows.append(_window(
            "2026-09-29T08:00:00Z", "2026-09-29T08:15:00Z"))
        resp = create_route(db, plan_id, req)
        assert resp.segment_ids == ["r2", "r3"]
        assert resp.arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert resp.total_wait_minutes == 0
    finally:
        db.close()


def test_waiting_at_origin_until_window_passes():
    db = _db()
    try:
        plan_id = _make_plan(db)
        # 无绕行：只能在 A 等待到 08:15 进入，08:25 到达
        req = _graph_request()
        req.segments[0].windows.append(_window(
            "2026-09-29T08:00:00Z", "2026-09-29T08:15:00Z"))
        req.segments = [req.segments[0]]
        resp = create_route(db, plan_id, req)
        assert resp.status == "success"
        assert resp.segment_ids == ["r1"]
        assert resp.arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
        assert resp.total_wait_minutes == 15
        assert len(resp.waits) == 1
        wt = resp.waits[0]
        assert (wt.node_id, wt.for_segment_id, wt.wait_minutes) == ("A", "r1", 15)
        assert wt.start_at.isoformat() == "2026-09-29T08:00:00+00:00"
        assert wt.end_at.isoformat() == "2026-09-29T08:15:00+00:00"
        win = wt.blocking_windows[0]
        assert win.start.isoformat() == "2026-09-29T08:00:00+00:00"
        assert win.end.isoformat() == "2026-09-29T08:15:00+00:00"
        step = resp.steps[0]
        assert step.departure_at.isoformat() == "2026-09-29T08:15:00+00:00"
        assert step.arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
        assert step.wait_before_minutes == 15
        # 持久化后等待记录仍在
        got = get_route(db, resp.route_id)
        assert got.waits[0].wait_minutes == 15
        assert got.steps[0].departure_at == step.departure_at
    finally:
        db.close()


def test_half_open_boundary_exact_arrival_at_window_start_ok():
    db = _db()
    try:
        plan_id = _make_plan(db)
        req = _graph_request()
        # r1 10 分钟，窗口 [08:10,08:20)：通行区间 [08:00,08:10) 右端点开，不相交
        req.segments[0].windows.append(_window(
            "2026-09-29T08:10:00Z", "2026-09-29T08:20:00Z"))
        req.segments = [req.segments[0]]
        resp = create_route(db, plan_id, req)
        assert resp.status == "success"
        assert resp.arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert resp.total_wait_minutes == 0
    finally:
        db.close()


def test_timezone_offset_converted_to_utc():
    db = _db()
    try:
        plan_id = _make_plan(db)
        req = _graph_request(departure_at="2026-09-29T16:00:00+08:00")  # 即 08:00 UTC
        req.segments[0].windows.append(_window(
            "2026-09-29T16:00:00+08:00", "2026-09-29T16:15:00+08:00"))
        req.segments = [req.segments[0]]
        resp = create_route(db, plan_id, req)
        assert resp.departure_at.isoformat() == "2026-09-29T08:00:00+00:00"
        assert resp.arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
        assert resp.total_wait_minutes == 15
    finally:
        db.close()


def test_embargo_from_plan_cargo_classes_blocks_route():
    db = _db()
    try:
        plan_id = _make_plan(db)
        req = _graph_request()
        # 仅保留直达，且禁运类别 3（方案原始货物含类别 3）
        req.segments[0].embargo_classes.append("3")
        req.segments = [req.segments[0]]
        resp = create_route(db, plan_id, req)
        assert resp.status == "infeasible"
        assert resp.steps == [] and resp.waits == [] and resp.segment_ids == []
        assert resp.arrival_at is None and resp.total_drive_minutes == 0
        assert len(resp.blockages) == 1
        b = resp.blockages[0]
        assert b.type == "embargo"
        assert (b.node_id, b.segment_id, b.to_node) == ("A", "r1", "Z")
        assert b.embargo_classes == ["3"] and b.matched_classes == ["3"]
        # 阻断结果同样持久化、可按编号查询
        got = get_route(db, resp.route_id)
        assert got.status == "infeasible"
        assert got.blockages[0].matched_classes == ["3"]
        assert list_plan_routes(plan_id, db)[0].status == "infeasible"
    finally:
        db.close()


def test_no_waiting_blockage_and_stable_order():
    db = _db()
    try:
        plan_id = _make_plan(db)
        # A 禁等；两条出边：z1 禁运类别 3（恒闭），a2 窗口在出发即闭
        req = _graph_request()
        req.nodes[0].allow_waiting = False
        req.segments = [
            req.segments[0].model_copy(update={
                "segment_id": "z1", "embargo_classes": ["3"],
                "windows": [_window("2026-09-29T08:00:00Z",
                                    "2026-09-29T09:00:00Z")]}),
            req.segments[1].model_copy(update={
                "segment_id": "a2", "to_node": "Z",
                "windows": [_window("2026-09-29T08:00:00Z",
                                    "2026-09-29T08:15:00Z")]}),
        ]
        resp = create_route(db, plan_id, req)
        assert resp.status == "infeasible"
        assert resp.steps == []  # 不得给出部分行程
        keys = [(b.type, b.node_id, b.segment_id) for b in resp.blockages]
        # embargo 优先，随后节点、路段 ID 字典序
        assert keys == [("embargo", "A", "z1"), ("no_waiting", "A", "a2")]
        nb = next(b for b in resp.blockages if b.type == "no_waiting")
        assert nb.arrival_at.isoformat() == "2026-09-29T08:00:00+00:00"
        assert nb.drive_minutes == 6
        assert nb.windows[0].start.isoformat() == "2026-09-29T08:00:00+00:00"
    finally:
        db.close()


def test_unreachable_reports_unreachable_blockage():
    db = _db()
    try:
        plan_id = _make_plan(db)
        req = _graph_request()
        req.nodes.append(RoadNodeIn(node_id="Q", allow_waiting=False))
        req.destination = "Q"  # Q 与任何路段都不相连
        resp = create_route(db, plan_id, req)
        assert resp.status == "infeasible"
        assert resp.blockages[0].type == "unreachable"
        assert (resp.blockages[0].origin, resp.blockages[0].destination) == ("A", "Q")
    finally:
        db.close()


def test_error_status_codes():
    db = _db()
    try:
        plan_id = _make_plan(db)
        # 非 success 方案 => 409
        req = PlanRequest(
            cargos=[
                {"cargo_id": "A1", "hazard_class": "3", "quantity": 10},
                {"cargo_id": "A2", "hazard_class": "5.1", "quantity": 10},
                {"cargo_id": "A3", "hazard_class": "6.1", "quantity": 10},
            ],
            compartments=[
                {"compartment_id": "1", "capacity": 30},
                {"compartment_id": "2", "capacity": 30},
            ],
            incompatibilities=[{"cargo_a": "A2", "cargo_b": "A3"}],
        )
        bad = create_plan(req, db)
        try:
            create_route(db, bad.plan_id, _graph_request())
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("非 success 方案应返回 409")
        try:
            create_route(db, 999999, _graph_request())
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("方案不存在应返回 404")
        try:
            get_route(db, 999999)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("路线不存在应返回 404")
        try:
            list_plan_routes(999999, db)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("方案不存在应返回 404")
    finally:
        db.close()


def test_request_validation_errors():
    # 起终点引用未知节点
    with _raises(ValidationError):
        _graph_request(destination="X")
    # 路段编号重复
    req = _graph_request()
    req.segments[1] = req.segments[1].model_copy(update={"segment_id": "r1"})
    with _raises(ValidationError):
        RouteRequest(**req.model_dump())
    # 路段引用未知节点
    req = _graph_request()
    req.segments[0] = req.segments[0].model_copy(update={"to_node": "X"})
    with _raises(ValidationError):
        RouteRequest(**req.model_dump())
    # 出发时间必须带时区
    with _raises(ValidationError):
        _graph_request(departure_at="2026-09-29T08:00:00")
    # 禁行窗口必须 end > start
    with _raises(ValidationError):
        RouteRequest(**{
            "nodes": [{"node_id": "A", "allow_waiting": True},
                      {"node_id": "Z", "allow_waiting": False}],
            "segments": [{"segment_id": "r1", "from_node": "A", "to_node": "Z",
                          "drive_minutes": 10,
                          "windows": [{"start": "2026-09-29T08:10:00Z",
                                       "end": "2026-09-29T08:00:00Z"}]}],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })


def test_non_minute_aligned_time_returns_422():
    db = _db()
    try:
        plan_id = _make_plan(db)
        try:
            create_route(db, plan_id, _graph_request(
                departure_at="2026-09-29T08:00:30Z"))
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("非整分钟时刻应返回 422")
    finally:
        db.close()


def test_route_endpoint_function_wraps_service():
    db = _db()
    try:
        plan_id = _make_plan(db)
        resp = create_plan_route(plan_id, _graph_request(), db)
        assert resp.status == "success"
        assert resp.plan_id == plan_id
    finally:
        db.close()


def _loop_graph_request():
    """全节点禁等 + S<->X 环 + BD 在 [08:00,08:17) 禁行：必须连绕两圈。"""
    return RouteRequest(**{
        "nodes": [
            {"node_id": "S", "allow_waiting": False},
            {"node_id": "X", "allow_waiting": False},
            {"node_id": "B", "allow_waiting": False},
            {"node_id": "D", "allow_waiting": False},
        ],
        "segments": [
            {"segment_id": "C1", "from_node": "S", "to_node": "X",
             "drive_minutes": 4, "embargo_classes": [], "windows": []},
            {"segment_id": "C2", "from_node": "X", "to_node": "S",
             "drive_minutes": 4, "embargo_classes": [], "windows": []},
            {"segment_id": "SB", "from_node": "S", "to_node": "B",
             "drive_minutes": 1, "embargo_classes": [], "windows": []},
            {"segment_id": "BD", "from_node": "B", "to_node": "D",
             "drive_minutes": 1, "embargo_classes": [],
             "windows": [{"start": "2026-09-29T08:00:00Z",
                          "end": "2026-09-29T08:17:00Z"}]},
        ],
        "origin": "S",
        "destination": "D",
        "departure_at": "2026-09-29T08:00:00Z",
    })


def test_no_waiting_loop_route_persisted_with_full_steps_and_zero_wait():
    # 回归：禁等节点图中需要重复经过同一批路段（C1、C2 各两次）消磨时间，
    # 恰在第 17 分钟进入 BD。响应须含完整逐段时刻、零等待记录并持久化。
    db = _db()
    try:
        plan_id = _make_plan(db)
        resp = create_route(db, plan_id, _loop_graph_request())
        assert resp.status == "success"
        assert resp.segment_ids == ["C1", "C2", "C1", "C2", "SB", "BD"]
        assert resp.arrival_at.isoformat() == "2026-09-29T08:18:00+00:00"
        assert resp.total_drive_minutes == 18
        assert resp.total_wait_minutes == 0
        assert resp.waits == [] and resp.blockages == []
        expected = [
            ("C1", "08:00", "08:04", 0),
            ("C2", "08:04", "08:08", 0),
            ("C1", "08:08", "08:12", 0),
            ("C2", "08:12", "08:16", 0),
            ("SB", "08:16", "08:17", 0),
            ("BD", "08:17", "08:18", 0),
        ]
        assert len(resp.steps) == 6
        for st, (sid, dep, arr, wait) in zip(resp.steps, expected):
            assert st.segment_id == sid
            assert st.departure_at.isoformat() == f"2026-09-29T{dep}:00+00:00"
            assert st.arrival_at.isoformat() == f"2026-09-29T{arr}:00+00:00"
            assert st.wait_before_minutes == wait
        # 持久化后逐段时刻与零等待记录仍可按编号查询
        got = get_route(db, resp.route_id)
        assert got.status == "success"
        assert got.segment_ids == resp.segment_ids
        assert got.arrival_at == resp.arrival_at
        assert [(s.segment_id, s.departure_at, s.arrival_at) for s in got.steps] == \
            [(s.segment_id, s.departure_at, s.arrival_at) for s in resp.steps]
        assert got.total_wait_minutes == 0 and got.waits == []
        assert list_plan_routes(plan_id, db)[-1].status == "success"
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

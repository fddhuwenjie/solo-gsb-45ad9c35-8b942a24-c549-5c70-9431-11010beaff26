"""运输进度模块测试：路线绑定、checkpoint 批次、幂等/回滚、延误重排、blocked/arrived。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi import HTTPException  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.executions import apply_event_batch, create_execution  # noqa: E402
from app.main import (  # noqa: E402
    bind_execution_route,
    create_plan,
    get_progress_state,
    submit_progress_checkpoints,
)
from app.progress import (  # noqa: E402
    apply_checkpoint_batch,
    bind_route,
    get_progress,
)
from app.route_service import create_route  # noqa: E402
from app.schemas import (  # noqa: E402
    CheckpointBatchIn,
    EventBatchIn,
    PlanRequest,
    ProgressBindIn,
    RouteRequest,
)
from app.seed import seed_rules  # noqa: E402

Base.metadata.create_all(bind=engine)
_seed_db = SessionLocal()
seed_rules(_seed_db)
_seed_db.close()


def _db():
    return SessionLocal()


def _http_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        return exc
    raise AssertionError("应抛出 HTTPException")


def _make_plan(db, cargo_id="G1", hazard="3") -> int:
    req = PlanRequest(
        cargos=[{"cargo_id": cargo_id, "hazard_class": hazard, "quantity": 10}],
        compartments=[{"compartment_id": "1", "capacity": 20}],
    )
    resp = create_plan(req, db)
    assert resp.status == "success"
    return resp.plan_id


def _make_infeasible_plan(db) -> int:
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
    return create_plan(req, db).plan_id


def _graph_request(**overrides):
    payload = {
        "nodes": [
            {"node_id": "A", "allow_waiting": True},
            {"node_id": "Z", "allow_waiting": False},
        ],
        "segments": [
            {"segment_id": "r1", "from_node": "A", "to_node": "Z",
             "drive_minutes": 10, "embargo_classes": [], "windows": []},
        ],
        "origin": "A",
        "destination": "Z",
        "departure_at": "2026-09-29T08:00:00Z",
    }
    payload.update(overrides)
    return RouteRequest(**payload)


def _chain_graph_request():
    """A->M(r1,6) -> Z(r2,4)，合计 10 分钟；直达 r0 12 分钟：计划选 r1+r2，08:10 到达。"""
    return RouteRequest(**{
        "nodes": [
            {"node_id": "A", "allow_waiting": True},
            {"node_id": "M", "allow_waiting": True},
            {"node_id": "Z", "allow_waiting": False},
        ],
        "segments": [
            {"segment_id": "r0", "from_node": "A", "to_node": "Z",
             "drive_minutes": 12, "embargo_classes": [], "windows": []},
            {"segment_id": "r1", "from_node": "A", "to_node": "M",
             "drive_minutes": 6, "embargo_classes": [], "windows": []},
            {"segment_id": "r2", "from_node": "M", "to_node": "Z",
             "drive_minutes": 4, "embargo_classes": [], "windows": []},
        ],
        "origin": "A",
        "destination": "Z",
        "departure_at": "2026-09-29T08:00:00Z",
    })


def _batch(*cps):
    return CheckpointBatchIn(checkpoints=list(cps))


def _cp(cid, seq, seg, at):
    return {"checkpoint_id": cid, "sequence": seq, "segment_id": seg, "arrived_at": at}


def _setup(db, req=None, plan_id=None):
    """创建方案、执行单、路线并绑定，返回 (plan_id, execution_id, route, progress)。"""
    plan_id = plan_id or _make_plan(db)
    execution = create_execution(db, plan_id)
    route = create_route(db, plan_id, req or _graph_request())
    progress = bind_route(db, execution.execution_id, ProgressBindIn(route_id=route.route_id))
    return plan_id, execution.execution_id, route, progress


# ---------- 绑定规则 ----------


def test_bind_success_creates_queryable_progress():
    db = _db()
    try:
        plan_id, ex_id, route, prog = _setup(db)
        assert prog.status == "in_transit"
        assert prog.execution_id == ex_id and prog.route_id == route.route_id
        assert prog.plan_id == plan_id
        assert prog.current_node == "A"
        assert prog.last_sequence == 0
        assert prog.completed == []
        assert prog.remaining_segment_ids == ["r1"]
        assert prog.remaining_steps[0].arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert prog.original_arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert prog.planned_arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert prog.delay_minutes == 0 and prog.blockages == []
        # 持久化可查询
        got = get_progress(db, prog.progress_id)
        assert got.progress_id == prog.progress_id
        assert got.remaining_segment_ids == ["r1"]
        # 端点函数同样可用
        assert get_progress_state(prog.progress_id, db).progress_id == prog.progress_id
    finally:
        db.close()


def test_bind_rejects_different_plan():
    db = _db()
    try:
        plan_a = _make_plan(db, "PA")
        ex_a = create_execution(db, plan_a)
        plan_b = _make_plan(db, "PB")
        route_b = create_route(db, plan_b, _graph_request())
        exc = _http_error(bind_route, db, ex_a.execution_id,
                          ProgressBindIn(route_id=route_b.route_id))
        assert exc.status_code == 409
        assert "方案不同" in exc.detail
    finally:
        db.close()


def test_bind_rejects_non_success_route():
    db = _db()
    try:
        plan_id = _make_plan(db)
        ex = create_execution(db, plan_id)
        bad_req = _graph_request(
            segments=[{"segment_id": "x1", "from_node": "A", "to_node": "Z",
                       "drive_minutes": 10, "embargo_classes": ["3"], "windows": []}],
        )
        route = create_route(db, plan_id, bad_req)
        assert route.status == "infeasible"
        exc = _http_error(bind_route, db, ex.execution_id,
                          ProgressBindIn(route_id=route.route_id))
        assert exc.status_code == 409 and "success" in exc.detail
    finally:
        db.close()


def test_bind_rejects_completed_execution():
    db = _db()
    try:
        plan_id = _make_plan(db, "GC")
        ex = create_execution(db, plan_id)
        apply_event_batch(db, ex.execution_id, EventBatchIn(events=[
            {"event_id": "e1", "sequence": 1, "cargo_id": "GC",
             "action": "load", "compartment_id": "1"},
        ]))
        apply_event_batch(db, ex.execution_id, EventBatchIn(events=[
            {"event_id": "e2", "sequence": 2, "cargo_id": "GC",
             "action": "unload", "compartment_id": "1"},
        ]))
        route = create_route(db, plan_id, _graph_request())
        exc = _http_error(bind_route, db, ex.execution_id,
                          ProgressBindIn(route_id=route.route_id))
        assert exc.status_code == 409 and "in_progress" in exc.detail
    finally:
        db.close()


def test_bind_is_unique_per_execution():
    db = _db()
    try:
        plan_id, ex_id, route, prog = _setup(db)
        exc = _http_error(bind_route, db, ex_id, ProgressBindIn(route_id=route.route_id))
        assert exc.status_code == 409 and "已绑定" in exc.detail
    finally:
        db.close()


def test_bind_404_for_missing_entities():
    db = _db()
    try:
        exc = _http_error(bind_route, db, 999999, ProgressBindIn(route_id=1))
        assert exc.status_code == 404
        plan_id = _make_plan(db, "G404")
        ex = create_execution(db, plan_id)
        exc = _http_error(bind_route, db, ex.execution_id, ProgressBindIn(route_id=999999))
        assert exc.status_code == 404
        exc = _http_error(get_progress, db, 999999)
        assert exc.status_code == 404
    finally:
        db.close()


def test_bind_endpoint_function_wraps_service():
    db = _db()
    try:
        plan_id = _make_plan(db, "GE")
        ex = create_execution(db, plan_id)
        route = create_route(db, plan_id, _graph_request())
        resp = bind_execution_route(ex.execution_id, ProgressBindIn(route_id=route.route_id), db)
        assert resp.status == "in_transit" and resp.route_id == route.route_id
    finally:
        db.close()


# ---------- checkpoint 基础推进与 arrived ----------


def test_on_time_checkpoint_arrives():
    db = _db()
    try:
        _, _, _, prog = _setup(db)
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c1", 1, "r1", "2026-09-29T08:10:00Z")))
        assert r.applied == 1 and r.skipped == 0
        assert r.status == "arrived"
        assert r.current_node == "Z" and r.last_sequence == 1
        assert r.remaining_segment_ids == [] and r.remaining_steps == []
        assert r.planned_arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        assert r.delay_minutes == 0
        c = r.completed[0]
        assert (c.checkpoint_id, c.sequence, c.segment_id) == ("c1", 1, "r1")
        assert (c.from_node, c.to_node) == ("A", "Z")
        assert c.drive_minutes == 10 and c.delay_minutes == 0
        assert c.arrived_at.isoformat() == "2026-09-29T08:10:00+00:00"
        # 端点函数同样可用
        ep = submit_progress_checkpoints(prog.progress_id,
                                         _batch(_cp("c1", 1, "r1", "2026-09-29T08:10:00Z")), db)
        assert ep.applied == 0 and ep.skipped == 1 and ep.status == "arrived"
    finally:
        db.close()


def test_chain_progresses_through_nodes():
    db = _db()
    try:
        _, _, route, prog = _setup(db, req=_chain_graph_request())
        assert route.segment_ids == ["r1", "r2"]
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        assert r.status == "in_transit" and r.current_node == "M"
        assert r.remaining_segment_ids == ["r2"]
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c2", 2, "r2", "2026-09-29T08:10:00Z")))
        assert r.status == "arrived" and r.current_node == "Z"
        assert [c.segment_id for c in r.completed] == ["r1", "r2"]
    finally:
        db.close()


def test_early_arrival_records_negative_segment_delay():
    db = _db()
    try:
        # 计划含 15 分钟等待（08:15 出发、08:25 到达）；物理最早 08:10 到达
        req = _graph_request(segments=[{
            "segment_id": "r1", "from_node": "A", "to_node": "Z", "drive_minutes": 10,
            "embargo_classes": [],
            "windows": [{"start": "2026-09-29T08:00:00Z",
                         "end": "2026-09-29T08:15:00Z"}],
        }])
        _, _, _, prog = _setup(db, req=req)
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c1", 1, "r1", "2026-09-29T08:10:00Z")))
        assert r.status == "arrived"
        assert r.completed[0].delay_minutes == -15
        assert r.delay_minutes == -15
    finally:
        db.close()


# ---------- 校验与整批回滚 ----------


def test_wrong_segment_rolls_back_batch():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 1, "r9", "2026-09-29T08:06:00Z")),
        )
        assert exc.status_code == 422 and "当前待行路段" in exc.detail["error"]
        assert exc.detail["checkpoint_id"] == "c1"
        assert exc.detail["state"]["last_sequence"] == 0
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 0 and got.completed == []
        assert got.current_node == "A" and got.remaining_segment_ids == ["r1", "r2"]
    finally:
        db.close()


def test_sequence_gap_rolls_back_batch():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 2, "r1", "2026-09-29T08:06:00Z")),
        )
        assert exc.status_code == 422 and "序号不连续" in exc.detail["error"]
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 0
    finally:
        db.close()


def test_time_regression_rolls_back_batch():
    db = _db()
    try:
        _, _, _, prog = _setup(db)
        # 早于出发时刻（08:00）=> 时间倒退
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 1, "r1", "2026-09-29T07:59:00Z")),
        )
        assert exc.status_code == 422 and "时间不得倒退" in exc.detail["error"]
        # 非整分钟
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 1, "r1", "2026-09-29T08:10:30Z")),
        )
        assert exc.status_code == 422 and "整分钟" in exc.detail["error"]
    finally:
        db.close()


def test_not_earlier_than_physical_arrival():
    db = _db()
    try:
        _, _, _, prog = _setup(db)
        # 行驶 10 分钟，08:00 出发，物理最早 08:10；08:09 不可能到达
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 1, "r1", "2026-09-29T08:09:00Z")),
        )
        assert exc.status_code == 422 and "物理最早到达时刻" in exc.detail["error"]
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 0
    finally:
        db.close()


def test_inter_checkpoint_time_monotone_and_physical():
    db = _db()
    try:
        req = _chain_graph_request()
        # r2 行驶 4 分钟，08:06 到 M：物理最早 08:10；08:08/08:09 不可能到达
        _, _, _, prog1 = _setup(db, req=req)
        apply_checkpoint_batch(db, prog1.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        exc = _http_error(
            apply_checkpoint_batch, db, prog1.progress_id,
            _batch(_cp("c2", 2, "r2", "2026-09-29T08:08:00Z")),
        )
        assert exc.status_code == 422 and "物理最早到达时刻" in exc.detail["error"]
        got = get_progress(db, prog1.progress_id)
        assert got.last_sequence == 1 and got.remaining_segment_ids == ["r2"]

        # 独立进度：到达时刻早于上一到达时刻 08:06 => 时间倒退
        _, _, _, prog2 = _setup(db, req=req)
        apply_checkpoint_batch(db, prog2.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        exc = _http_error(
            apply_checkpoint_batch, db, prog2.progress_id,
            _batch(_cp("c2", 2, "r2", "2026-09-29T08:05:00Z")),
        )
        assert exc.status_code == 422 and "时间不得倒退" in exc.detail["error"]

        # 物理最早时刻恰好到达允许（半开下界，含等号）
        _, _, _, prog3 = _setup(db, req=req)
        apply_checkpoint_batch(db, prog3.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        r = apply_checkpoint_batch(db, prog3.progress_id,
                                   _batch(_cp("c2", 2, "r2", "2026-09-29T08:10:00Z")))
        assert r.status == "arrived"
    finally:
        db.close()


def test_any_error_in_batch_rolls_back_whole_batch():
    db = _db()
    try:
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": True},
                {"node_id": "M", "allow_waiting": True},
                {"node_id": "N", "allow_waiting": True},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s2", "from_node": "M", "to_node": "N",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s3", "from_node": "N", "to_node": "Z",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, _, prog = _setup(db, req=req)
        # 第一条合法、第二条路段不匹配 => 整批回滚
        exc = _http_error(apply_checkpoint_batch, db, prog.progress_id, _batch(
            _cp("a", 1, "s1", "2026-09-29T08:05:00Z"),
            _cp("b", 2, "s1", "2026-09-29T08:10:00Z"),
        ))
        assert exc.status_code == 422 and "当前待行路段" in exc.detail["error"]
        assert exc.detail["state"]["last_sequence"] == 0
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 0 and got.completed == []
        assert got.current_node == "A"
    finally:
        db.close()


def test_no_new_checkpoint_after_arrived():
    db = _db()
    try:
        _, _, _, prog = _setup(db)
        apply_checkpoint_batch(db, prog.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:10:00Z")))
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c2", 2, "r1", "2026-09-29T08:20:00Z")),
        )
        assert exc.status_code == 422 and "arrived" in exc.detail["error"]
    finally:
        db.close()


# ---------- 幂等与冲突 ----------


def test_idempotent_retry_same_payload():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        payload = _cp("c1", 1, "r1", "2026-09-29T08:06:00Z")
        r1 = apply_checkpoint_batch(db, prog.progress_id, _batch(payload))
        assert r1.applied == 1
        r2 = apply_checkpoint_batch(db, prog.progress_id, _batch(payload))
        assert r2.applied == 0 and r2.skipped == 1
        assert r2.last_sequence == 1 and r2.current_node == "M"
        # arrived 后的相同载荷重试同样幂等
        apply_checkpoint_batch(db, prog.progress_id,
                               _batch(_cp("c2", 2, "r2", "2026-09-29T08:10:00Z")))
        r3 = apply_checkpoint_batch(db, prog.progress_id,
                                    _batch(_cp("c2", 2, "r2", "2026-09-29T08:10:00Z")))
        assert r3.applied == 0 and r3.skipped == 1 and r3.status == "arrived"
        got = get_progress(db, prog.progress_id)
        assert len(got.completed) == 2
    finally:
        db.close()


def test_retry_mixed_with_new_checkpoint():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        apply_checkpoint_batch(db, prog.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        r = apply_checkpoint_batch(db, prog.progress_id, _batch(
            _cp("c1", 1, "r1", "2026-09-29T08:06:00Z"),  # 重试跳过
            _cp("c2", 2, "r2", "2026-09-29T08:10:00Z"),  # 新应用
        ))
        assert r.applied == 1 and r.skipped == 1 and r.status == "arrived"
    finally:
        db.close()


def test_same_checkpoint_id_different_payload_conflict():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        apply_checkpoint_batch(db, prog.progress_id,
                               _batch(_cp("c1", 1, "r1", "2026-09-29T08:06:00Z")))
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("c1", 1, "r1", "2026-09-29T08:07:00Z")),
        )
        assert exc.status_code == 409
        assert exc.detail["checkpoint_id"] == "c1"
        assert exc.detail["recorded"]["arrived_at"].endswith("08:06:00+00:00")
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 1  # 冲突未推进
    finally:
        db.close()


def test_in_batch_duplicate_id_same_payload_skipped():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        cp = _cp("c1", 1, "r1", "2026-09-29T08:06:00Z")
        r = apply_checkpoint_batch(db, prog.progress_id, _batch(cp, dict(cp)))
        assert r.applied == 1 and r.skipped == 1 and r.last_sequence == 1
        assert len(get_progress(db, prog.progress_id).completed) == 1
    finally:
        db.close()


def test_in_batch_duplicate_id_different_payload_conflict_and_rollback():
    db = _db()
    try:
        _, _, _, prog = _setup(db, req=_chain_graph_request())
        exc = _http_error(apply_checkpoint_batch, db, prog.progress_id, _batch(
            _cp("x", 1, "r1", "2026-09-29T08:06:00Z"),
            _cp("x", 1, "r1", "2026-09-29T08:07:00Z"),
        ))
        assert exc.status_code == 409
        got = get_progress(db, prog.progress_id)
        assert got.last_sequence == 0 and got.completed == []
    finally:
        db.close()


# ---------- 延误重排 ----------


def test_delay_replans_remaining_with_wait():
    db = _db()
    try:
        # 计划：A->M 08:05；r2 窗口 [08:10,09:00)，[08:05,08:10) 半开不相交，08:10 到 Z。
        # 实际 08:08 到 M（延误 3）：[08:08,08:13) 命中窗口，M 允许等待 -> 等到 09:00，09:05 到 Z。
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": True},
                {"node_id": "M", "allow_waiting": True},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "r1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "r2", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 5, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:10:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, route, prog = _setup(db, req=req)
        assert route.arrival_at.isoformat() == "2026-09-29T08:10:00+00:00"
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c1", 1, "r1", "2026-09-29T08:08:00Z")))
        assert r.status == "in_transit" and r.current_node == "M"
        assert r.remaining_segment_ids == ["r2"]
        step = r.remaining_steps[0]
        assert step.departure_at.isoformat() == "2026-09-29T09:00:00+00:00"
        assert step.arrival_at.isoformat() == "2026-09-29T09:05:00+00:00"
        assert step.wait_before_minutes == 52
        assert r.planned_arrival_at.isoformat() == "2026-09-29T09:05:00+00:00"
        assert r.delay_minutes == 55  # 相对原始计划到达 08:10
        assert len(r.remaining_waits) == 1
        wt = r.remaining_waits[0]
        assert (wt.node_id, wt.for_segment_id, wt.wait_minutes) == ("M", "r2", 52)
        assert r.completed[0].delay_minutes == 3
        # 按调整后计划抵达
        r2 = apply_checkpoint_batch(db, prog.progress_id,
                                    _batch(_cp("c2", 2, "r2", "2026-09-29T09:05:00Z")))
        assert r2.status == "arrived" and r2.delay_minutes == 55
        got = get_progress(db, prog.progress_id)
        assert got.status == "arrived" and [c.segment_id for c in got.completed] == ["r1", "r2"]
    finally:
        db.close()


def test_delay_replan_picks_alternative_route():
    db = _db()
    try:
        # 计划：s1(A->M,5) + d1(M->Z,5)，d1 窗口 [08:10,09:00)：08:05 到 M，
        # 通行区间 [08:05,08:10) 与窗口半开不相交，08:10 到 Z（早于无窗绕行 d2 的 08:25）。
        # 实际 08:08 到 M（延误 3）：d1 的 [08:08,08:13) 命中窗口，M 可等到 09:00
        # -> 09:05 到达；无窗绕行 d2(20) 08:28 到达，更早 => 重排选 d2。
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": True},
                {"node_id": "M", "allow_waiting": True},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "d1", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 5, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:10:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
                {"segment_id": "d2", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 20, "embargo_classes": [], "windows": []},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, route, prog = _setup(db, req=req)
        assert route.segment_ids == ["s1", "d1"]
        r = apply_checkpoint_batch(db, prog.progress_id,
                                   _batch(_cp("c1", 1, "s1", "2026-09-29T08:08:00Z")))
        assert r.status == "in_transit"
        assert r.remaining_segment_ids == ["d2"]
        assert r.remaining_steps[0].departure_at.isoformat() == "2026-09-29T08:08:00+00:00"
        assert r.remaining_steps[0].arrival_at.isoformat() == "2026-09-29T08:28:00+00:00"
        assert r.planned_arrival_at.isoformat() == "2026-09-29T08:28:00+00:00"
        assert r.delay_minutes == 18  # 原计划 08:10 到达
        assert r.remaining_waits == []
        got = get_progress(db, prog.progress_id)
        assert got.remaining_segment_ids == ["d2"] and got.delay_minutes == 18
    finally:
        db.close()


def test_replan_infeasible_sets_blocked_and_batch_not_committed():
    db = _db()
    try:
        # A->M(s1,5)，M 禁等；s2 M->Z(1)，窗口 [08:06,09:00)。
        # 计划：08:05 到 M，s2 通行 [08:05,08:06) 与窗口半开不相交，08:06 到 Z（success）。
        # 实际 08:07 到 M（延误 2）：[08:07,08:08) 命中窗口且 M 禁等 => 重排无解 => blocked。
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": False},
                {"node_id": "M", "allow_waiting": False},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s2", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 1, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:06:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, route, prog = _setup(db, req=req)
        assert route.status == "success"
        assert route.arrival_at.isoformat() == "2026-09-29T08:06:00+00:00"
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("d1", 1, "s1", "2026-09-29T08:07:00Z")),
        )
        assert exc.status_code == 422
        assert "blocked" in exc.detail["error"]
        assert exc.detail["checkpoint_id"] == "d1"
        assert exc.detail["blockages"][0]["type"] == "no_waiting"
        assert exc.detail["state"]["status"] == "blocked"
        # 进度已置 blocked 且持久化（可查询），但该批 checkpoint 不提交
        got = get_progress(db, prog.progress_id)
        assert got.status == "blocked"
        assert got.last_sequence == 0 and got.completed == []
        assert got.current_node == "A"  # 批次未提交，仍在 s1 的上游
        assert got.remaining_segment_ids == ["s1", "s2"]
        assert got.planned_arrival_at is None
        assert got.delay_minutes == 2
        assert len(got.blockages) == 1 and got.blockages[0].type == "no_waiting"
    finally:
        db.close()


def test_blocked_progress_rejects_new_checkpoints_but_allows_retry():
    db = _db()
    try:
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": False},
                {"node_id": "M", "allow_waiting": False},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s2", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 1, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:06:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, _, prog = _setup(db, req=req)
        _http_error(apply_checkpoint_batch, db, prog.progress_id,
                    _batch(_cp("d1", 1, "s1", "2026-09-29T08:07:00Z")))
        # blocked 后新 checkpoint 一律 422
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("d1", 1, "s1", "2026-09-29T08:07:00Z")),
        )
        # 该 checkpoint 从未提交（整批回滚），故不是幂等重试而是 blocked 拒绝
        assert exc.status_code == 422 and "blocked" in exc.detail["error"]
        got = get_progress(db, prog.progress_id)
        assert got.status == "blocked" and got.last_sequence == 0
    finally:
        db.close()


def test_blocked_mid_batch_restores_whole_pre_batch_plan():
    db = _db()
    try:
        # A->M(s1,5) -> N(s2,1) -> Z(s3,1)，全节点禁等。
        # 计划：08:05 到 M、08:06 到 N；s3 窗口 [08:07,09:00)，通行 [08:06,08:07)
        # 半开不相交，08:07 到 Z（success）。
        # 同批：s1 准点到 M、s2 延误至 08:08 到 N => 重排 s3 命中窗口且 N 禁等 => blocked。
        # 整批不提交：进度回滚到提交前（current_node=A、三段全部仍待行、last_sequence=0）。
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": False},
                {"node_id": "M", "allow_waiting": False},
                {"node_id": "N", "allow_waiting": False},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s2", "from_node": "M", "to_node": "N",
                 "drive_minutes": 1, "embargo_classes": [], "windows": []},
                {"segment_id": "s3", "from_node": "N", "to_node": "Z",
                 "drive_minutes": 1, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:07:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, route, prog = _setup(db, req=req)
        assert route.segment_ids == ["s1", "s2", "s3"]
        exc = _http_error(apply_checkpoint_batch, db, prog.progress_id, _batch(
            _cp("a", 1, "s1", "2026-09-29T08:05:00Z"),
            _cp("b", 2, "s2", "2026-09-29T08:08:00Z"),
        ))
        assert exc.status_code == 422
        assert exc.detail["state"]["last_sequence"] == 0
        got = get_progress(db, prog.progress_id)
        assert got.status == "blocked"
        assert got.last_sequence == 0 and got.completed == []
        assert got.current_node == "A"
        assert got.remaining_segment_ids == ["s1", "s2", "s3"]
        assert got.planned_arrival_at is None
        assert got.delay_minutes == 2  # 最近一次延误（s2 实际 08:08 vs 计划 08:06）
        assert got.blockages[0].type == "no_waiting"
    finally:
        db.close()


def test_replan_respects_embargo_classes():
    db = _db()
    try:
        # 实际延误后唯一备选绕行路段对载货类别 3 恒闭 => blocked(embargo)
        req = RouteRequest(**{
            "nodes": [
                {"node_id": "A", "allow_waiting": False},
                {"node_id": "M", "allow_waiting": False},
                {"node_id": "Z", "allow_waiting": False},
            ],
            "segments": [
                {"segment_id": "s1", "from_node": "A", "to_node": "M",
                 "drive_minutes": 5, "embargo_classes": [], "windows": []},
                {"segment_id": "s2", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 1, "embargo_classes": [],
                 "windows": [{"start": "2026-09-29T08:06:00Z",
                              "end": "2026-09-29T09:00:00Z"}]},
                {"segment_id": "s3", "from_node": "M", "to_node": "Z",
                 "drive_minutes": 8, "embargo_classes": ["3"], "windows": []},
            ],
            "origin": "A", "destination": "Z",
            "departure_at": "2026-09-29T08:00:00Z",
        })
        _, _, _, prog = _setup(db, req=req)
        exc = _http_error(
            apply_checkpoint_batch, db, prog.progress_id,
            _batch(_cp("d1", 1, "s1", "2026-09-29T08:07:00Z")),
        )
        assert exc.status_code == 422
        types = {b["type"] for b in exc.detail["blockages"]}
        assert "embargo" in types
        got = get_progress(db, prog.progress_id)
        assert got.status == "blocked"
        assert any(b.type == "embargo" and b.matched_classes == ["3"]
                   for b in got.blockages)
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

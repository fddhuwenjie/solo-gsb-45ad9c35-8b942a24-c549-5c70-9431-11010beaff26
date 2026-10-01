"""运输进度测试：绑定、checkpoint 批次、幂等、整批回滚、延误重排、blocked 与 arrived。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.executions import apply_event_batch, create_execution  # noqa: E402
from app.main import (  # noqa: E402
    create_plan,
    create_route_progress,
    get_progress_state,
    submit_progress_checkpoints,
)
from app.progress import apply_checkpoint_batch, create_progress, get_progress  # noqa: E402
from app.route_service import create_route  # noqa: E402
from app.schemas import (  # noqa: E402
    CheckpointBatchIn,
    CheckpointIn,
    EventBatchIn,
    PlanRequest,
    ProgressCreateIn,
    RouteRequest,
)
from app.seed import seed_rules  # noqa: E402

Base.metadata.create_all(bind=engine)
_seed_db = SessionLocal()
seed_rules(_seed_db)
_seed_db.close()


def _db():
    return SessionLocal()


def _http_error(fn, *args):
    """调用并断言抛出 HTTPException，返回该异常。"""
    try:
        fn(*args)
    except HTTPException as exc:
        return exc
    raise AssertionError("应抛出 HTTPException")


def _make_plan(db) -> int:
    """success 方案：G1/G3 类别 3、G2 类别 5.1。"""
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


# 图 A：r2 窗口 [08:20,08:40)，计划走 r1+r2（08:20 到，绕行 r3+r4 为 08:21）；
# 晚到 M 后 r2 撞窗，须重排为绕行 r3+r4（08:23 到）。
GRAPH_A = {
    "nodes": [
        {"node_id": "A", "allow_waiting": True},
        {"node_id": "M", "allow_waiting": True},
        {"node_id": "N", "allow_waiting": False},
        {"node_id": "Z", "allow_waiting": False},
    ],
    "segments": [
        {"segment_id": "r1", "from_node": "A", "to_node": "M", "drive_minutes": 10,
         "embargo_classes": [], "windows": []},
        {"segment_id": "r2", "from_node": "M", "to_node": "Z", "drive_minutes": 10,
         "embargo_classes": [],
         "windows": [{"start": "2026-09-29T08:20:00Z", "end": "2026-09-29T08:40:00Z"}]},
        {"segment_id": "r3", "from_node": "M", "to_node": "N", "drive_minutes": 3,
         "embargo_classes": [], "windows": []},
        {"segment_id": "r4", "from_node": "N", "to_node": "Z", "drive_minutes": 8,
         "embargo_classes": [], "windows": []},
    ],
    "origin": "A",
    "destination": "Z",
    "departure_at": "2026-09-29T08:00:00Z",
}

# 图 B：全节点禁等，r2 窗口 [08:20,09:00)；晚到 M 后重排无解 -> blocked。
GRAPH_B = {
    "nodes": [
        {"node_id": "A", "allow_waiting": False},
        {"node_id": "M", "allow_waiting": False},
        {"node_id": "Z", "allow_waiting": False},
    ],
    "segments": [
        {"segment_id": "r1", "from_node": "A", "to_node": "M", "drive_minutes": 10,
         "embargo_classes": [], "windows": []},
        {"segment_id": "r2", "from_node": "M", "to_node": "Z", "drive_minutes": 10,
         "embargo_classes": [],
         "windows": [{"start": "2026-09-29T08:20:00Z", "end": "2026-09-29T09:00:00Z"}]},
    ],
    "origin": "A",
    "destination": "Z",
    "departure_at": "2026-09-29T08:00:00Z",
}

# 图 C：r1 窗口 [08:00,08:15)，计划在 A 等待到 08:15 出发，08:35 到。
GRAPH_C = {
    "nodes": [
        {"node_id": "A", "allow_waiting": True},
        {"node_id": "M", "allow_waiting": False},
        {"node_id": "Z", "allow_waiting": False},
    ],
    "segments": [
        {"segment_id": "r1", "from_node": "A", "to_node": "M", "drive_minutes": 10,
         "embargo_classes": [],
         "windows": [{"start": "2026-09-29T08:00:00Z", "end": "2026-09-29T08:15:00Z"}]},
        {"segment_id": "r2", "from_node": "M", "to_node": "Z", "drive_minutes": 10,
         "embargo_classes": [], "windows": []},
    ],
    "origin": "A",
    "destination": "Z",
    "departure_at": "2026-09-29T08:00:00Z",
}

# 图 D：无窗口直达两段，计划 08:20 到；晚到后剩余计划整体平移。
GRAPH_D = {
    "nodes": [
        {"node_id": "A", "allow_waiting": False},
        {"node_id": "M", "allow_waiting": False},
        {"node_id": "Z", "allow_waiting": False},
    ],
    "segments": [
        {"segment_id": "r1", "from_node": "A", "to_node": "M", "drive_minutes": 10,
         "embargo_classes": [], "windows": []},
        {"segment_id": "r2", "from_node": "M", "to_node": "Z", "drive_minutes": 10,
         "embargo_classes": [], "windows": []},
    ],
    "origin": "A",
    "destination": "Z",
    "departure_at": "2026-09-29T08:00:00Z",
}

# 图 E：唯一路段禁运类别 3（方案货物含类别 3）-> 路线 infeasible。
GRAPH_E = {
    "nodes": [
        {"node_id": "A", "allow_waiting": False},
        {"node_id": "Z", "allow_waiting": False},
    ],
    "segments": [
        {"segment_id": "r1", "from_node": "A", "to_node": "Z", "drive_minutes": 10,
         "embargo_classes": ["3"], "windows": []},
    ],
    "origin": "A",
    "destination": "Z",
    "departure_at": "2026-09-29T08:00:00Z",
}


def _make_route(db, plan_id: int, graph: dict):
    return create_route(db, plan_id, RouteRequest(**graph))


def _make_progress(db, graph=None):
    """创建 方案 -> 执行单 -> 路线 -> 进度，返回 (progress, route, execution)。"""
    plan_id = _make_plan(db)
    execution = create_execution(db, plan_id)
    route = _make_route(db, plan_id, graph or GRAPH_A)
    assert route.status == "success"
    progress = create_progress(db, route.route_id, ProgressCreateIn(execution_id=execution.execution_id))
    return progress, route, execution


def _batch(items):
    return CheckpointBatchIn(checkpoints=items)


def _cp(checkpoint_id, sequence, segment_id, arrived_at):
    return {"checkpoint_id": checkpoint_id, "sequence": sequence,
            "segment_id": segment_id, "arrived_at": arrived_at}


def test_bind_creates_progress_with_route_steps():
    db = _db()
    try:
        progress, route, execution = _make_progress(db)
        assert progress.status == "in_progress"
        assert progress.route_id == route.route_id
        assert progress.execution_id == execution.execution_id
        assert progress.plan_id == route.plan_id
        assert progress.last_sequence == 0
        assert progress.delay_minutes == 0
        assert progress.current_node == "A"
        assert progress.planned_arrival_at.isoformat() == "2026-09-29T08:20:00+00:00"
        assert progress.expected_arrival_at.isoformat() == "2026-09-29T08:20:00+00:00"
        # 初始剩余计划即路线逐段时刻
        assert [s.segment_id for s in progress.remaining_plan] == ["r1", "r2"]
        assert progress.remaining_plan[0].departure_at.isoformat() == "2026-09-29T08:00:00+00:00"
        assert progress.remaining_plan[1].arrival_at.isoformat() == "2026-09-29T08:20:00+00:00"
        assert progress.completed_segments == [] and progress.checkpoints == []
        # 可查询
        got = get_progress(db, progress.progress_id)
        assert got.status == "in_progress"
        assert [s.segment_id for s in got.remaining_plan] == ["r1", "r2"]
    finally:
        db.close()


def test_bind_rejects_non_success_route():
    db = _db()
    try:
        plan_id = _make_plan(db)
        execution = create_execution(db, plan_id)
        route = _make_route(db, plan_id, GRAPH_E)
        assert route.status == "infeasible"
        exc = _http_error(create_progress, db, route.route_id,
                          ProgressCreateIn(execution_id=execution.execution_id))
        assert exc.status_code == 409
        assert "success" in exc.detail
    finally:
        db.close()


def test_bind_rejects_completed_execution():
    db = _db()
    try:
        plan_id = _make_plan(db)
        execution = create_execution(db, plan_id)
        planned = {"G1": "1", "G2": "2", "G3": "1"}
        events = [
            {"event_id": f"e{i}", "sequence": i, "cargo_id": cid,
             "action": "load", "compartment_id": comp}
            for i, (cid, comp) in enumerate(sorted(planned.items()), start=1)
        ] + [
            {"event_id": f"u{i}", "sequence": 3 + i, "cargo_id": cid,
             "action": "unload", "compartment_id": comp}
            for i, (cid, comp) in enumerate(sorted(planned.items()), start=1)
        ]
        r = apply_event_batch(db, execution.execution_id, EventBatchIn(events=events))
        assert r.status == "completed"
        route = _make_route(db, plan_id, GRAPH_A)
        exc = _http_error(create_progress, db, route.route_id,
                          ProgressCreateIn(execution_id=execution.execution_id))
        assert exc.status_code == 409
        assert "in_progress" in exc.detail
    finally:
        db.close()


def test_bind_rejects_cross_plan():
    db = _db()
    try:
        plan1 = _make_plan(db)
        plan2 = _make_plan(db)
        route = _make_route(db, plan1, GRAPH_A)
        execution2 = create_execution(db, plan2)
        exc = _http_error(create_progress, db, route.route_id,
                          ProgressCreateIn(execution_id=execution2.execution_id))
        assert exc.status_code == 409
        assert "不同方案" in exc.detail
    finally:
        db.close()


def test_bind_rejects_duplicate():
    db = _db()
    try:
        progress, route, execution = _make_progress(db)
        # 同一路线重复绑定
        exc = _http_error(create_progress, db, route.route_id,
                          ProgressCreateIn(execution_id=execution.execution_id))
        assert exc.status_code == 409
        # 同一执行单绑定另一条路线
        route2 = _make_route(db, progress.plan_id, GRAPH_D)
        exc = _http_error(create_progress, db, route2.route_id,
                          ProgressCreateIn(execution_id=execution.execution_id))
        assert exc.status_code == 409
    finally:
        db.close()


def test_bind_not_found():
    db = _db()
    try:
        progress, route, execution = _make_progress(db)
        exc = _http_error(create_progress, db, 999999,
                          ProgressCreateIn(execution_id=execution.execution_id))
        assert exc.status_code == 404
        exc = _http_error(create_progress, db, route.route_id,
                          ProgressCreateIn(execution_id=999999))
        assert exc.status_code == 404
        exc = _http_error(get_progress, db, 999999)
        assert exc.status_code == 404
    finally:
        db.close()


def test_apply_checkpoints_on_time_to_arrived():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
        ]))
        assert r.applied == 1 and r.skipped == 0
        assert r.status == "in_progress"
        assert r.last_sequence == 1
        assert r.delay_minutes == 0
        assert [s.segment_id for s in r.remaining_plan] == ["r2"]
        assert [s.segment_id for s in r.completed_segments] == ["r1"]

        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-2", 2, "r2", "2026-09-29T08:20:00Z"),
        ]))
        assert r.status == "arrived"
        assert r.delay_minutes == 0
        assert r.expected_arrival_at.isoformat() == "2026-09-29T08:20:00+00:00"
        assert r.remaining_plan == []
        # 持久化可查
        got = get_progress(db, progress.progress_id)
        assert got.status == "arrived"
        assert got.current_node == "Z"
        assert [c.checkpoint_id for c in got.checkpoints] == ["cp-1", "cp-2"]
        assert [s.segment_id for s in got.completed_segments] == ["r1", "r2"]
    finally:
        db.close()


def test_early_arrival_keeps_plan():
    db = _db()
    try:
        progress, route, _ = _make_progress(db, GRAPH_C)
        # 计划：r1 等到 08:15 进、08:25 到 M；r2 08:25->08:35
        assert route.total_wait_minutes == 15
        assert progress.expected_arrival_at.isoformat() == "2026-09-29T08:35:00+00:00"
        # 实际 08:20 到 M（早于计划 08:25）：剩余计划不变，无延误
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:20:00Z"),
        ]))
        assert r.delay_minutes == 0
        assert [s.segment_id for s in r.remaining_plan] == ["r2"]
        assert r.remaining_plan[0].arrival_at.isoformat() == "2026-09-29T08:35:00+00:00"
        # 实际 08:30 到 Z（早于计划 08:35）：arrived，延误为 0
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-2", 2, "r2", "2026-09-29T08:30:00Z"),
        ]))
        assert r.status == "arrived"
        assert r.delay_minutes == 0
        assert r.expected_arrival_at.isoformat() == "2026-09-29T08:30:00+00:00"
    finally:
        db.close()


def test_late_arrival_replans_same_route():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        # r1 实际 08:15 到 M（计划 08:10）：从 M 重排，r2 平移为 08:15->08:25
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:15:00Z"),
        ]))
        assert r.status == "in_progress"
        assert r.delay_minutes == 5
        assert r.expected_arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
        assert len(r.remaining_plan) == 1
        step = r.remaining_plan[0]
        assert step.segment_id == "r2"
        assert step.departure_at.isoformat() == "2026-09-29T08:15:00+00:00"
        assert step.arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
        # 完成第二段：arrived，延误保持 5 分钟
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-2", 2, "r2", "2026-09-29T08:25:00Z"),
        ]))
        assert r.status == "arrived"
        assert r.delay_minutes == 5
        got = get_progress(db, progress.progress_id)
        assert got.delay_minutes == 5
        assert got.expected_arrival_at.isoformat() == "2026-09-29T08:25:00+00:00"
    finally:
        db.close()


def test_late_arrival_replans_detour():
    db = _db()
    try:
        progress, route, _ = _make_progress(db, GRAPH_A)
        assert [s.segment_id for s in route.steps] == ["r1", "r2"]
        # r1 实际 08:12 到 M（计划 08:10）：r2 撞窗 [08:20,08:40)，
        # 从 M 重排为绕行 r3+r4（08:23 到），延误 3 分钟
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:12:00Z"),
        ]))
        assert r.status == "in_progress"
        assert r.delay_minutes == 3
        assert r.expected_arrival_at.isoformat() == "2026-09-29T08:23:00+00:00"
        assert [s.segment_id for s in r.remaining_plan] == ["r3", "r4"]
        assert r.remaining_plan[0].departure_at.isoformat() == "2026-09-29T08:12:00+00:00"
        assert r.remaining_plan[0].arrival_at.isoformat() == "2026-09-29T08:15:00+00:00"
        assert r.remaining_plan[1].arrival_at.isoformat() == "2026-09-29T08:23:00+00:00"
        assert [s.segment_id for s in r.completed_segments] == ["r1"]
        # 按新待行路段继续上报直至终点
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-2", 2, "r3", "2026-09-29T08:15:00Z"),
            _cp("cp-3", 3, "r4", "2026-09-29T08:23:00Z"),
        ]))
        assert r.status == "arrived"
        assert r.delay_minutes == 3
        assert r.remaining_plan == []
        got = get_progress(db, progress.progress_id)
        assert got.status == "arrived"
        assert [s.segment_id for s in got.completed_segments] == ["r1", "r3", "r4"]
    finally:
        db.close()


def test_replan_no_solution_blocks_and_batch_not_committed():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_B)
        # r1 实际 08:25 到 M：r2 窗口 [08:20,09:00) 关闭且 M 禁等 -> 重排无解
        r = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:25:00Z"),
        ]))
        assert r.status == "blocked"
        assert r.applied == 0  # 整批不提交
        assert r.last_sequence == 0
        assert r.completed_segments == []
        assert len(r.blockages) == 1
        b = r.blockages[0]
        assert b.type == "no_waiting"
        assert (b.node_id, b.segment_id) == ("M", "r2")
        # 持久化：进度为 blocked，checkpoint 未落库，剩余计划保持提交前
        got = get_progress(db, progress.progress_id)
        assert got.status == "blocked"
        assert got.last_sequence == 0
        assert got.checkpoints == []
        assert [s.segment_id for s in got.remaining_plan] == ["r1", "r2"]
        assert got.blockages[0].type == "no_waiting"
        # blocked 后不再接收上报
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:25:00Z"),
        ]))
        assert exc.status_code == 409
    finally:
        db.close()


def test_idempotent_retry_same_payload():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        r1 = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
        ]))
        assert r1.applied == 1
        # 相同 checkpoint_id + 相同载荷重试：不重复推进（带时区写法不同也算相同）
        r2 = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T16:10:00+08:00"),
        ]))
        assert r2.applied == 0 and r2.skipped == 1
        assert r2.last_sequence == 1
        # 重试与新 checkpoint 混排：重试跳过，新 checkpoint 按序应用
        r3 = apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
            _cp("cp-2", 2, "r2", "2026-09-29T08:20:00Z"),
        ]))
        assert r3.applied == 1 and r3.skipped == 1
        assert r3.status == "arrived"
        got = get_progress(db, progress.progress_id)
        assert len(got.checkpoints) == 2
    finally:
        db.close()


def test_same_checkpoint_id_different_payload_conflict():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
        ]))
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:11:00Z"),  # 载荷不同
        ]))
        assert exc.status_code == 409
        assert exc.detail["checkpoint_id"] == "cp-1"
        assert exc.detail["recorded"]["arrived_at"] == "2026-09-29T08:10:00+00:00"
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 1
    finally:
        db.close()


def test_sequence_gap_rolls_back_batch():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
            _cp("cp-2", 3, "r2", "2026-09-29T08:20:00Z"),  # 跳过 2
        ]))
        assert exc.status_code == 422
        assert "序号不连续" in exc.detail["error"]
        assert exc.detail["state"]["last_sequence"] == 0
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 0 and got.checkpoints == []
    finally:
        db.close()


def test_segment_mismatch_rolls_back_batch():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
            _cp("cp-2", 2, "r1", "2026-09-29T08:20:00Z"),  # 当前待行路段为 r2
        ]))
        assert exc.status_code == 422
        assert "当前待行路段为 r2" in exc.detail["error"]
        assert exc.detail["checkpoint_id"] == "cp-2"
        # 整批回滚：第一条合法 checkpoint 也未生效
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 0 and got.checkpoints == []
        assert [s.segment_id for s in got.remaining_plan] == ["r1", "r2"]
    finally:
        db.close()


def test_time_regression_rejected():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
        ]))
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-2", 2, "r2", "2026-09-29T08:05:00Z"),  # 早于上一到达时刻
        ]))
        assert exc.status_code == 422
        assert "时间不得倒退" in exc.detail["error"]
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 1
    finally:
        db.close()


def test_physical_earliest_rejected():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        # 08:00 出发，r1 行驶 10 分钟，物理最早 08:10 到 M
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:05:00Z"),
        ]))
        assert exc.status_code == 422
        assert "物理到达时刻" in exc.detail["error"]
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 0
    finally:
        db.close()


def test_batch_replan_then_mismatch_uses_new_pending():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_A)
        # 同批：cp-1 晚到触发重排（待行变为 r3），cp-2 仍报旧计划的 r2 -> 违规，整批回滚
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:12:00Z"),
            _cp("cp-2", 2, "r2", "2026-09-29T08:22:00Z"),
        ]))
        assert exc.status_code == 422
        assert "当前待行路段为 r3" in exc.detail["error"]
        got = get_progress(db, progress.progress_id)
        assert got.last_sequence == 0 and got.checkpoints == []
        assert got.status == "in_progress"  # 回滚后未受影响
    finally:
        db.close()


def test_arrived_then_reject():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        apply_checkpoint_batch(db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
            _cp("cp-2", 2, "r2", "2026-09-29T08:20:00Z"),
        ]))
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-3", 3, "r2", "2026-09-29T08:20:00Z"),
        ]))
        assert exc.status_code == 409
        assert "抵达终点" in exc.detail
    finally:
        db.close()


def test_non_minute_aligned_arrived_at_rejected():
    db = _db()
    try:
        progress, _, _ = _make_progress(db, GRAPH_D)
        exc = _http_error(apply_checkpoint_batch, db, progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:30Z"),
        ]))
        assert exc.status_code == 422
        assert "整分钟" in exc.detail["error"]
    finally:
        db.close()


def test_naive_arrived_at_rejected():
    try:
        CheckpointIn(**_cp("cp-1", 1, "r1", "2026-09-29T08:10:00"))
    except ValidationError:
        pass
    else:
        raise AssertionError("不带时区的到达时刻应校验失败")


def test_endpoint_functions_wrap_service():
    db = _db()
    try:
        plan_id = _make_plan(db)
        execution = create_execution(db, plan_id)
        route = _make_route(db, plan_id, GRAPH_D)
        progress = create_route_progress(
            route.route_id, ProgressCreateIn(execution_id=execution.execution_id), db)
        assert progress.status == "in_progress"
        r = submit_progress_checkpoints(progress.progress_id, _batch([
            _cp("cp-1", 1, "r1", "2026-09-29T08:10:00Z"),
        ]), db)
        assert r.applied == 1
        got = get_progress_state(progress.progress_id, db)
        assert got.progress_id == progress.progress_id
        assert got.last_sequence == 1
        assert [s.segment_id for s in got.completed_segments] == ["r1"]
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

"""现场执行模块测试：执行单创建、事件批次应用、幂等重试、整批回滚、自动完成。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi import HTTPException  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.executions import apply_event_batch, create_execution, get_execution  # noqa: E402
from app.main import create_plan  # noqa: E402
from app.schemas import EventBatchIn, PlanRequest  # noqa: E402
from app.seed import seed_rules  # noqa: E402

Base.metadata.create_all(bind=engine)
_seed_db = SessionLocal()
seed_rules(_seed_db)  # 与服务启动一致：初始化类别级禁配规则
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
    """创建一个 success 方案：G1/G3 -> 舱1，G2 -> 舱2。"""
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
    planned = {a.cargo_id: a.compartment_id for a in resp.assignments}
    assert planned == {"G1": "1", "G2": "2", "G3": "1"}
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
    resp = create_plan(req, db)
    assert resp.status == "infeasible"
    return resp.plan_id


def _batch(events):
    return EventBatchIn(events=events)


def _load_events(plan):
    """按计划舱室生成全部装载事件。"""
    return [
        {"event_id": f"e{i}", "sequence": i, "cargo_id": cid,
         "action": "load", "compartment_id": comp}
        for i, (cid, comp) in enumerate(sorted(plan.items()), start=1)
    ]


PLANNED = {"G1": "1", "G2": "2", "G3": "1"}


def test_create_execution_freezes_assignments():
    db = _db()
    try:
        plan_id = _make_plan(db)
        ex = create_execution(db, plan_id)
        assert ex.plan_id == plan_id
        assert ex.status == "in_progress"
        assert ex.last_sequence == 0
        states = {c.cargo_id: (c.planned_compartment_id, c.status) for c in ex.cargos}
        assert states == {
            "G1": ("1", "pending"),
            "G2": ("2", "pending"),
            "G3": ("1", "pending"),
        }
    finally:
        db.close()


def test_create_execution_rejects_non_success_plan():
    db = _db()
    try:
        plan_id = _make_infeasible_plan(db)
        exc = _http_error(create_execution, db, plan_id)
        assert exc.status_code == 409
        assert "success" in exc.detail
        exc = _http_error(create_execution, db, 999999)
        assert exc.status_code == 404
    finally:
        db.close()


def test_apply_load_events_in_order():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        r = apply_event_batch(db, ex.execution_id, _batch(_load_events(PLANNED)[:2]))
        assert r.applied == 2 and r.skipped == 0
        assert r.last_sequence == 2
        states = {c.cargo_id: c.status for c in r.cargos}
        assert states == {"G1": "loaded", "G2": "loaded", "G3": "pending"}
        # 持久化可查
        got = get_execution(db, ex.execution_id)
        assert got.last_sequence == 2
        assert [e.event_id for e in got.events] == ["e1", "e2"]
    finally:
        db.close()


def test_wrong_compartment_rolls_back_batch():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        events = _load_events(PLANNED)
        events[1]["compartment_id"] = "1"  # G2 计划舱室为 2
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(events))
        assert exc.status_code == 422
        assert "计划舱室" in exc.detail["error"]
        assert exc.detail["event_id"] == "e2"
        # 提交前状态随错误返回
        assert exc.detail["state"]["last_sequence"] == 0
        # 整批回滚：第一条合法事件也未生效
        got = get_execution(db, ex.execution_id)
        assert got.last_sequence == 0
        assert all(c.status == "pending" for c in got.cargos)
        assert got.events == []
    finally:
        db.close()


def test_sequence_gap_rolls_back_batch():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        events = _load_events(PLANNED)
        events[1]["sequence"] = 3  # 跳过 2
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(events))
        assert exc.status_code == 422
        assert "序号不连续" in exc.detail["error"]
        got = get_execution(db, ex.execution_id)
        assert got.last_sequence == 0 and got.events == []
    finally:
        db.close()


def test_duplicate_load_rejected():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        apply_event_batch(db, ex.execution_id, _batch(_load_events(PLANNED)[:1]))
        dup = [{"event_id": "e2", "sequence": 2, "cargo_id": "G1",
                "action": "load", "compartment_id": "1"}]
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(dup))
        assert exc.status_code == 422
        assert "重复装载" in exc.detail["error"]
        assert exc.detail["state"]["last_sequence"] == 1
    finally:
        db.close()


def test_unload_before_load_rejected():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        ev = [{"event_id": "e1", "sequence": 1, "cargo_id": "G1",
               "action": "unload", "compartment_id": "1"}]
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(ev))
        assert exc.status_code == 422
        assert "不能开始卸载" in exc.detail["error"]
    finally:
        db.close()


def test_unload_requires_all_loaded_first():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        apply_event_batch(db, ex.execution_id, _batch(_load_events(PLANNED)[:2]))
        # G3 尚未装载，此时卸载 G1 违规
        ev = [{"event_id": "e3", "sequence": 3, "cargo_id": "G1",
               "action": "unload", "compartment_id": "1"}]
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(ev))
        assert exc.status_code == 422
        assert "不能开始卸载" in exc.detail["error"]
        # 未装先卸：全部装完后，对 pending 货物直接卸也违规（此处用新执行单验证）
        ex2 = create_execution(db, _make_plan(db))
        apply_event_batch(db, ex2.execution_id, _batch(_load_events(PLANNED)))
        bad = [{"event_id": "u1", "sequence": 4, "cargo_id": "G1",
                "action": "unload", "compartment_id": "1"},
               {"event_id": "u2", "sequence": 5, "cargo_id": "G1",
                "action": "unload", "compartment_id": "1"}]
        exc = _http_error(apply_event_batch, db, ex2.execution_id, _batch(bad))
        assert exc.status_code == 422
        assert "重复卸载" in exc.detail["error"]
        got = get_execution(db, ex2.execution_id)
        assert got.last_sequence == 3  # 整批回滚
    finally:
        db.close()


def test_idempotent_retry_same_payload():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        events = _load_events(PLANNED)
        r1 = apply_event_batch(db, ex.execution_id, _batch(events[:2]))
        assert r1.applied == 2
        # 相同 event_id + 相同载荷重试：不重复推进
        r2 = apply_event_batch(db, ex.execution_id, _batch(events[:2]))
        assert r2.applied == 0 and r2.skipped == 2
        assert r2.last_sequence == 2
        # 重试与新事件混排：重试跳过，新事件按序应用
        r3 = apply_event_batch(db, ex.execution_id, _batch([events[0], events[2]]))
        assert r3.applied == 1 and r3.skipped == 1
        assert r3.last_sequence == 3
        got = get_execution(db, ex.execution_id)
        assert len(got.events) == 3
    finally:
        db.close()


def test_same_event_id_different_payload_conflict():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        apply_event_batch(db, ex.execution_id, _batch(_load_events(PLANNED)[:1]))
        changed = [{"event_id": "e1", "sequence": 1, "cargo_id": "G1",
                    "action": "load", "compartment_id": "2"}]  # 载荷不同
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(changed))
        assert exc.status_code == 409
        assert exc.detail["event_id"] == "e1"
        assert exc.detail["recorded"]["compartment_id"] == "1"
        got = get_execution(db, ex.execution_id)
        assert got.last_sequence == 1
    finally:
        db.close()


def test_full_cycle_auto_completed():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        apply_event_batch(db, ex.execution_id, _batch(_load_events(PLANNED)))
        unloads = [
            {"event_id": f"u{i}", "sequence": 3 + i, "cargo_id": cid,
             "action": "unload", "compartment_id": comp}
            for i, (cid, comp) in enumerate(sorted(PLANNED.items()), start=1)
        ]
        r = apply_event_batch(db, ex.execution_id, _batch(unloads[:2]))
        assert r.status == "in_progress"
        r = apply_event_batch(db, ex.execution_id, _batch(unloads[2:]))
        assert r.status == "completed"
        assert r.last_sequence == 6
        assert all(c.status == "unloaded" for c in r.cargos)
        # 完成后状态可查询
        got = get_execution(db, ex.execution_id)
        assert got.status == "completed"
        assert len(got.events) == 6
    finally:
        db.close()


def test_unknown_cargo_and_execution_404():
    db = _db()
    try:
        ex = create_execution(db, _make_plan(db))
        ev = [{"event_id": "e1", "sequence": 1, "cargo_id": "GX",
               "action": "load", "compartment_id": "1"}]
        exc = _http_error(apply_event_batch, db, ex.execution_id, _batch(ev))
        assert exc.status_code == 422
        assert "不在执行单中" in exc.detail["error"]
        exc = _http_error(apply_event_batch, db, 999999, _batch(ev))
        assert exc.status_code == 404
        exc = _http_error(get_execution, db, 999999)
        assert exc.status_code == 404
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

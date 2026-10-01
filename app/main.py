"""危险化学品同车分舱规划 API。"""
import json
import logging
from contextlib import asynccontextmanager
from itertools import combinations

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .executions import apply_event_batch, create_execution, get_execution
from .models import Plan, SegregationRule
from .progress import apply_checkpoint_batch, create_progress, get_progress
from .route_service import create_route, get_route, list_routes_for_plan
from .schemas import (
    AssignmentOut,
    CheckpointBatchIn,
    CheckpointBatchResult,
    CompartmentLoad,
    ConflictOut,
    EventBatchIn,
    EventBatchResult,
    ExecutionOut,
    PlanRequest,
    PlanResponse,
    PlanSummary,
    ProgressCreateIn,
    ProgressOut,
    RouteRequest,
    RouteResponse,
    RouteSummary,
    RuleOut,
)
from .seed import seed_rules
from .solver import Cargo, Compartment, Incompatibility, solve_plan

logger = logging.getLogger("hazmat")


def natural_key(s: str):
    """编号排序键：纯数字按数值排序，其余按字符串排序，保证稳定一致。"""
    return (0, int(s)) if s.isdigit() else (1, s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        inserted = seed_rules(db)
        if inserted:
            logger.info("已初始化 %d 条示例禁配规则", inserted)
    finally:
        db.close()
    yield


app = FastAPI(
    title="危险化学品同车分舱规划 API",
    description="基于 OR-Tools CP-SAT 的分舱规划（最少舱室优先，禁配隔离，结果稳定可复现），"
                "支持方案现场执行（扫描事件按序应用、幂等重试、整批回滚），"
                "危险品运输路线编排（禁运类别、UTC 半开禁行窗口、节点等待，"
                "按最早到达/总等待最少/路段序列字典序确定唯一结果），"
                "以及运输进度跟踪（success 路线绑定同方案执行单、checkpoint 按序上报、"
                "延误时按保存的道路图从当前节点重排剩余路段）。",
    version="1.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rules", response_model=list[RuleOut])
def list_rules(db: Session = Depends(get_db)):
    """查看系统内置的类别级禁配规则。"""
    return db.query(SegregationRule).order_by(SegregationRule.id).all()


def _build_incompatibilities(req: PlanRequest, db: Session) -> list[Incompatibility]:
    """合并类别级默认规则与请求中的货物级禁配关系。"""
    incs: list[Incompatibility] = []
    if req.use_default_rules:
        rules = db.query(SegregationRule).all()
        rule_map = {frozenset((r.class_a, r.class_b)): r for r in rules}
        for a, b in combinations(req.cargos, 2):
            rule = rule_map.get(frozenset((a.hazard_class, b.hazard_class)))
            if rule is not None:
                incs.append(Incompatibility(
                    a.cargo_id,
                    b.cargo_id,
                    f"类别 {rule.class_a} 与 {rule.class_b} 禁配（{rule.description}）",
                ))
    for inc in req.incompatibilities:
        incs.append(Incompatibility(inc.cargo_a, inc.cargo_b, inc.rule or "请求指定禁配"))
    return incs


def _solve_to_response(plan_id: int, req: PlanRequest, db: Session) -> dict:
    """执行求解并组装响应字典（plan_id/created_at 由调用方补充）。"""
    cargos = sorted(
        (Cargo(c.cargo_id, c.hazard_class, c.quantity) for c in req.cargos),
        key=lambda c: natural_key(c.cargo_id),
    )
    compartments = sorted(
        (Compartment(c.compartment_id, c.capacity) for c in req.compartments),
        key=lambda c: natural_key(c.compartment_id),
    )
    incompatibilities = _build_incompatibilities(req, db)
    forced = {a.cargo_id: a.compartment_id for a in req.assignments}
    forbidden = {(a.cargo_id, a.compartment_id) for a in req.forbidden_assignments}

    result = solve_plan(cargos, compartments, incompatibilities, forced, forbidden,
                        req.time_limit_seconds)

    assignments: list[AssignmentOut] = []
    loads: list[CompartmentLoad] = []
    if result.status == "success":
        assignments = [
            AssignmentOut(cargo_id=cid, compartment_id=result.assignments[cid])
            for cid in sorted(result.assignments, key=natural_key)
        ]
        qty = {c.cargo_id: c.quantity for c in cargos}
        by_comp: dict[str, list[str]] = {}
        for cid, comp_id in result.assignments.items():
            by_comp.setdefault(comp_id, []).append(cid)
        caps = {c.compartment_id: c.capacity for c in compartments}
        loads = [
            CompartmentLoad(
                compartment_id=comp_id,
                load=sum(qty[c] for c in cids),
                capacity=caps[comp_id],
                cargo_ids=sorted(cids, key=natural_key),
            )
            for comp_id, cids in sorted(by_comp.items(), key=lambda kv: natural_key(kv[0]))
        ]

    return {
        "plan_id": plan_id,
        "status": result.status,
        "proven_optimal": result.proven_optimal,
        "stable_order_proven": result.stable_order_proven,
        "compartments_used": result.compartments_used,
        "assignments": [a.model_dump() for a in assignments],
        "compartment_loads": [l.model_dump() for l in loads],
        "conflicts": result.conflicts,
    }


@app.post("/plans", response_model=PlanResponse)
def create_plan(req: PlanRequest, db: Session = Depends(get_db)):
    """创建分舱规划方案。

    - 优先使用最少舱室，再按舱室编号、货物编号稳定分配，相同输入结果一致；
    - 仅当 CP-SAT 证明不可行时 status=infeasible，assignments 为空，
      conflicts 列出冲突货物与规则；
    - 第一阶段在时限内未得出结论（UNKNOWN）时 status=undetermined，
      assignments 与 conflicts 均为空，不会误判为不可行；
    - 第二阶段超时时回退第一阶段完整解，status=success 且
      proven_optimal=false、stable_order_proven=false。
    """
    # 先求解再落库，plan_id 使用数据库自增编号
    payload = _solve_to_response(plan_id=0, req=req, db=db)
    plan = Plan(
        status=payload["status"],
        request_json=req.model_dump_json(),
        result_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    payload["plan_id"] = plan.id
    plan.result_json = json.dumps(payload, ensure_ascii=False)
    db.commit()
    return PlanResponse(**payload, created_at=plan.created_at)


@app.get("/plans", response_model=list[PlanSummary])
def list_plans(db: Session = Depends(get_db)):
    """列出全部方案摘要。"""
    plans = db.query(Plan).order_by(Plan.id).all()
    out = []
    for p in plans:
        result = json.loads(p.result_json)
        out.append(PlanSummary(
            plan_id=p.id,
            status=p.status,
            compartments_used=result.get("compartments_used", 0),
            created_at=p.created_at,
        ))
    return out


@app.get("/plans/{plan_id}", response_model=PlanResponse)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    """按方案编号查询规划结果。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在")
    result = json.loads(plan.result_json)
    result["plan_id"] = plan.id
    return PlanResponse(**result, created_at=plan.created_at)


# ---------- 现场执行 ----------


@app.post("/plans/{plan_id}/executions", response_model=ExecutionOut)
def create_plan_execution(plan_id: int, db: Session = Depends(get_db)):
    """为方案创建现场执行单。

    仅 status=success 的方案可创建（否则 409）；创建时冻结方案中的
    货物与舱室分配，所有货物初始状态为 pending，last_sequence 从 0 开始。
    """
    return create_execution(db, plan_id)


@app.post("/executions/{execution_id}/events", response_model=EventBatchResult)
def submit_execution_events(execution_id: int, batch: EventBatchIn, db: Session = Depends(get_db)):
    """批量提交扫描事件，同一事务中按序应用。

    - sequence 从 1 起连续递增，新事件必须接续 last_sequence；
    - 货物必须装入计划舱室，不得重复装载、未装先卸，开始卸载前须完成全部装载；
    - 任一新事件违规则整批回滚，返回 422、首个错误与提交前状态；
    - 相同 event_id 且载荷相同的重试不重复推进；载荷不同返回 409；
    - 全部货物卸完后执行单自动置为 completed。
    """
    return apply_event_batch(db, execution_id, batch)


@app.get("/executions/{execution_id}", response_model=ExecutionOut)
def get_execution_state(execution_id: int, db: Session = Depends(get_db)):
    """查询执行单：最后序号、每件货物状态与已应用事件日志。"""
    return get_execution(db, execution_id)


# ---------- 危险品运输路线编排 ----------


@app.post("/plans/{plan_id}/routes", response_model=RouteResponse, status_code=201)
def create_plan_route(plan_id: int, req: RouteRequest, db: Session = Depends(get_db)):
    """为 success 方案编排危险品运输路线。

    - 危险类别从方案的原始货物自动提取，路段禁运类别与之相交则恒闭；
    - 禁行窗口按 [start,end) 半开处理，通行区间与其相交即不可进入；
      可在 allow_waiting=true 的节点等待到下一可行时刻；
    - 唯一结果按 最早到达 → 总等待最少 → 路段 ID 序列字典序 裁决；
    - 无可行路线时 status=infeasible，steps/waits 为空，blockages 为
      稳定排序的阻断原因，不返回部分行程。结果持久化，可按编号查询。
    """
    return create_route(db, plan_id, req)


@app.get("/plans/{plan_id}/routes", response_model=list[RouteSummary])
def list_plan_routes(plan_id: int, db: Session = Depends(get_db)):
    """列出某方案下的全部运输路线摘要。"""
    return list_routes_for_plan(db, plan_id)


@app.get("/routes/{route_id}", response_model=RouteResponse)
def get_route_state(route_id: int, db: Session = Depends(get_db)):
    """按路线编号查询编排结果（逐段时刻、等待记录或阻断原因）。"""
    return get_route(db, route_id)


# ---------- 运输进度 ----------


@app.post("/routes/{route_id}/progress", response_model=ProgressOut, status_code=201)
def create_route_progress(route_id: int, req: ProgressCreateIn, db: Session = Depends(get_db)):
    """把 success 路线绑定到同方案的 in_progress 执行单，创建可查询的运输进度。

    仅 success 路线可绑定；执行单须为同方案的 in_progress 单——不同方案、
    非 success 路线或已完成执行单不得绑定（409）；路线与执行单均只能绑定
    一次。初始剩余计划为路线的逐段时刻，状态为 in_progress。
    """
    return create_progress(db, route_id, req)


@app.post("/progress/{progress_id}/checkpoints", response_model=CheckpointBatchResult)
def submit_progress_checkpoints(
    progress_id: int, batch: CheckpointBatchIn, db: Session = Depends(get_db)
):
    """批量上报 checkpoint，同一事务中按序应用。

    - sequence 从 1 起连续递增，segment_id 必须匹配当前待行路段；
    - arrived_at 不倒退，且不早于该段物理到达时刻（上一到达 + 行驶分钟）；
    - 相同 checkpoint_id 且载荷相同的重试幂等跳过；载荷不同返回 409；
    - 任一新 checkpoint 违规：整批回滚（422），返回首个错误与提交前状态；
    - 实际到达晚于计划时，依据路线保存的道路图、禁运类别、禁行窗口与节点
      等待规则从当前节点重排剩余路段，返回已完成段、调整后的剩余计划及延误；
      重排无解则进度置为 blocked 且整批不提交；抵达终点后为 arrived。
    """
    return apply_checkpoint_batch(db, progress_id, batch)


@app.get("/progress/{progress_id}", response_model=ProgressOut)
def get_progress_state(progress_id: int, db: Session = Depends(get_db)):
    """查询运输进度：状态、已完成段、当前剩余计划、延误与 checkpoint 日志。"""
    return get_progress(db, progress_id)

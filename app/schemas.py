"""Pydantic 请求/响应模型与校验。"""
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class CargoIn(BaseModel):
    cargo_id: str = Field(min_length=1, max_length=64, description="货物编号")
    hazard_class: str = Field(min_length=1, max_length=16, description="危险类别，如 3、5.1")
    quantity: int = Field(gt=0, description="数量（占用舱室容量的单位数）")


class CompartmentIn(BaseModel):
    compartment_id: str = Field(min_length=1, max_length=64, description="舱室编号")
    capacity: int = Field(gt=0, description="舱室容量")


class IncompatibilityIn(BaseModel):
    cargo_a: str = Field(min_length=1, max_length=64)
    cargo_b: str = Field(min_length=1, max_length=64)
    rule: Optional[str] = Field(default=None, max_length=255, description="规则说明（可选）")


class AssignmentIn(BaseModel):
    cargo_id: str = Field(min_length=1, max_length=64)
    compartment_id: str = Field(min_length=1, max_length=64)


class PlanRequest(BaseModel):
    cargos: list[CargoIn] = Field(min_length=1, description="货物清单")
    compartments: list[CompartmentIn] = Field(min_length=1, description="舱室清单")
    incompatibilities: list[IncompatibilityIn] = Field(default_factory=list, description="货物级禁配关系（补充默认规则）")
    assignments: list[AssignmentIn] = Field(default_factory=list, description="指定舱室条件：货物必须放入该舱室")
    forbidden_assignments: list[AssignmentIn] = Field(default_factory=list, description="指定舱室条件：货物禁止放入该舱室")
    use_default_rules: bool = Field(default=True, description="是否启用系统内置的类别级禁配规则")
    time_limit_seconds: float = Field(default=30.0, gt=0, le=300, description="求解时间上限（秒）")

    @model_validator(mode="after")
    def check_references(self):
        cargo_ids = [c.cargo_id for c in self.cargos]
        if len(set(cargo_ids)) != len(cargo_ids):
            raise ValueError("货物编号重复")
        comp_ids = [c.compartment_id for c in self.compartments]
        if len(set(comp_ids)) != len(comp_ids):
            raise ValueError("舱室编号重复")
        cargo_set, comp_set = set(cargo_ids), set(comp_ids)
        for inc in self.incompatibilities:
            if inc.cargo_a == inc.cargo_b:
                raise ValueError(f"禁配关系两端不能是同一货物: {inc.cargo_a}")
            for cid in (inc.cargo_a, inc.cargo_b):
                if cid not in cargo_set:
                    raise ValueError(f"禁配关系引用了不存在的货物: {cid}")
        for a in self.assignments + self.forbidden_assignments:
            if a.cargo_id not in cargo_set:
                raise ValueError(f"指定舱室条件引用了不存在的货物: {a.cargo_id}")
            if a.compartment_id not in comp_set:
                raise ValueError(f"指定舱室条件引用了不存在的舱室: {a.compartment_id}")
        forced = {}
        for a in self.assignments:
            if a.cargo_id in forced and forced[a.cargo_id] != a.compartment_id:
                raise ValueError(f"货物 {a.cargo_id} 被指定到多个不同舱室")
            forced[a.cargo_id] = a.compartment_id
        return self


class AssignmentOut(BaseModel):
    cargo_id: str
    compartment_id: str


class CompartmentLoad(BaseModel):
    compartment_id: str
    load: int
    capacity: int
    cargo_ids: list[str]


class ConflictOut(BaseModel):
    type: str = Field(description="冲突类型：capacity / incompatibility / assignment / mixed")
    cargo_ids: list[str] = Field(description="导致冲突的货物编号")
    rule: str = Field(description="触发冲突的规则")
    detail: str = Field(description="冲突说明")


class PlanResponse(BaseModel):
    plan_id: int
    status: str = Field(description="success / infeasible / undetermined")
    proven_optimal: bool = Field(description="是否证明最优（时间限制内）")
    stable_order_proven: bool = Field(
        default=False,
        description="第二阶段稳定次序是否证得最优（第二阶段超时回退时为 false）",
    )
    compartments_used: int
    assignments: list[AssignmentOut]
    compartment_loads: list[CompartmentLoad]
    conflicts: list[ConflictOut]
    created_at: datetime


class PlanSummary(BaseModel):
    plan_id: int
    status: str
    compartments_used: int
    created_at: datetime


class RuleOut(BaseModel):
    id: int
    class_a: str
    class_b: str
    level: str
    description: str

    model_config = {"from_attributes": True}


# ---------- 现场执行 ----------


class ScanEventIn(BaseModel):
    """一条扫描事件：event_id 幂等，sequence 从 1 起连续递增。"""

    event_id: str = Field(min_length=1, max_length=64, description="事件编号（幂等键）")
    sequence: int = Field(ge=1, description="连续序号，从 1 开始逐条递增")
    cargo_id: str = Field(min_length=1, max_length=64, description="货物编号")
    action: Literal["load", "unload"] = Field(description="动作：load 装舱 / unload 卸舱")
    compartment_id: str = Field(min_length=1, max_length=64, description="扫描到的舱室编号")


class EventBatchIn(BaseModel):
    """一批扫描事件：同一事务内按序应用，任一违规则整批回滚。"""

    events: list[ScanEventIn] = Field(min_length=1, description="扫描事件批次")


class CargoStateOut(BaseModel):
    cargo_id: str
    planned_compartment_id: str = Field(description="创建执行单时冻结的计划舱室")
    status: str = Field(description="pending（未装）/ loaded（在舱）/ unloaded（已卸）")


class EventOut(BaseModel):
    event_id: str
    sequence: int
    cargo_id: str
    action: str
    compartment_id: str


class ExecutionOut(BaseModel):
    execution_id: int
    plan_id: int
    status: str = Field(description="in_progress / completed（全部卸完自动完成）")
    last_sequence: int = Field(description="已应用的最后连续序号")
    cargos: list[CargoStateOut]
    events: list[EventOut] = Field(description="已应用的事件日志（按序号升序）")
    created_at: datetime
    updated_at: datetime


class EventBatchResult(BaseModel):
    """事件批次处理结果：applied 为新应用条数，skipped 为幂等重试跳过条数。"""

    execution_id: int
    applied: int
    skipped: int
    status: str
    last_sequence: int
    cargos: list[CargoStateOut]


# ---------- 危险品运输路线编排 ----------


def _aware_utc(v: datetime) -> datetime:
    """要求带时区信息（UTC 禁行窗口需要明确时刻）。"""
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("必须提供带时区信息的时间（建议 UTC，如 2026-09-29T08:00:00Z）")
    return v


class RestrictionWindowIn(BaseModel):
    """UTC 禁行窗口，按半开区间 [start, end) 处理。"""

    start: datetime = Field(description="窗口开始（UTC，带时区），包含该时刻")
    end: datetime = Field(description="窗口结束（UTC，带时区），不包含该时刻")

    @field_validator("start", "end")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _check_order(self):
        if self.end <= self.start:
            raise ValueError("禁行窗口结束时刻必须晚于开始时刻")
        return self


class RoadSegmentIn(BaseModel):
    """有向道路路段。"""

    segment_id: str = Field(min_length=1, max_length=64, description="路段编号")
    from_node: str = Field(min_length=1, max_length=64, description="起点节点编号")
    to_node: str = Field(min_length=1, max_length=64, description="终点节点编号")
    drive_minutes: int = Field(ge=1, description="行驶分钟（正整数）")
    embargo_classes: list[str] = Field(
        default_factory=list, description="禁运危险类别（恒闭，等待不能解除）"
    )
    windows: list[RestrictionWindowIn] = Field(
        default_factory=list, description="UTC 禁行窗口 [start, end)，相交即不可进入"
    )


class RoadNodeIn(BaseModel):
    """道路节点及是否允许等待。"""

    node_id: str = Field(min_length=1, max_length=64, description="节点编号")
    allow_waiting: bool = Field(default=False, description="是否允许在该节点等待下一可行时刻")


class RouteRequest(BaseModel):
    """路线编排请求；危险类别自动从方案原始货物提取，请求中不提供。"""

    nodes: list[RoadNodeIn] = Field(min_length=1, description="道路节点清单")
    segments: list[RoadSegmentIn] = Field(min_length=1, description="有向路段清单")
    origin: str = Field(min_length=1, max_length=64, description="起点节点编号")
    destination: str = Field(min_length=1, max_length=64, description="终点节点编号")
    departure_at: datetime = Field(description="出发时间（带时区，内部按 UTC 计算）")

    @field_validator("departure_at")
    @classmethod
    def _departure_aware(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _check_graph(self):
        node_ids = [n.node_id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("节点编号重复")
        seg_ids = [s.segment_id for s in self.segments]
        if len(set(seg_ids)) != len(seg_ids):
            raise ValueError("路段编号重复")
        node_set = set(node_ids)
        for ref in (self.origin, self.destination):
            if ref not in node_set:
                raise ValueError(f"起终点引用了不存在的节点: {ref}")
        for s in self.segments:
            if s.from_node not in node_set:
                raise ValueError(f"路段 {s.segment_id} 的起点节点不存在: {s.from_node}")
            if s.to_node not in node_set:
                raise ValueError(f"路段 {s.segment_id} 的终点节点不存在: {s.to_node}")
        return self


class RestrictionWindowOut(BaseModel):
    start: datetime
    end: datetime


class RouteStepOut(BaseModel):
    segment_id: str
    from_node: str
    to_node: str
    departure_at: datetime = Field(description="进入路段的 UTC 时刻")
    arrival_at: datetime = Field(description="到达下游节点的 UTC 时刻")
    drive_minutes: int
    wait_before_minutes: int = Field(description="在上游节点为本段等待的分钟数")


class RouteWaitOut(BaseModel):
    node_id: str
    for_segment_id: str
    start_at: datetime = Field(description="到达节点（开始等待）的 UTC 时刻")
    end_at: datetime = Field(description="实际驶离的 UTC 时刻")
    wait_minutes: int
    blocking_windows: list[RestrictionWindowOut] = Field(description="导致等待的禁行窗口")


class RouteBlockageOut(BaseModel):
    type: str = Field(
        description="阻断类型：embargo（类别禁运）/ no_waiting（窗口关闭且节点禁等）/ unreachable（无可行路线）"
    )
    node_id: Optional[str] = None
    segment_id: Optional[str] = None
    to_node: Optional[str] = None
    embargo_classes: list[str] = Field(default_factory=list)
    matched_classes: list[str] = Field(default_factory=list, description="与本车载货类别相交的禁运类别")
    arrival_at: Optional[datetime] = Field(default=None, description="最早到达该节点的 UTC 时刻（no_waiting）")
    drive_minutes: Optional[int] = None
    windows: list[RestrictionWindowOut] = Field(default_factory=list)
    origin: Optional[str] = None
    destination: Optional[str] = None


class RouteResponse(BaseModel):
    route_id: int
    plan_id: int
    status: str = Field(description="success / infeasible（无可行路线，steps 与 waits 必为空）")
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: Optional[datetime] = Field(default=None, description="最早到达终点的 UTC 时刻")
    total_drive_minutes: int
    total_wait_minutes: int
    segment_ids: list[str] = Field(description="唯一结果的路段 ID 序列（字典序裁决依据）")
    hazard_classes: list[str] = Field(description="从方案原始货物提取的危险类别")
    steps: list[RouteStepOut]
    waits: list[RouteWaitOut]
    blockages: list[RouteBlockageOut] = Field(description="不可行时稳定排序的阻断原因")
    created_at: datetime


class RouteSummary(BaseModel):
    route_id: int
    plan_id: int
    status: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: Optional[datetime] = None
    total_wait_minutes: int
    created_at: datetime


# ---------- 危险品运输进度 ----------


class ProgressBindIn(BaseModel):
    """把一条 success 路线绑定到执行单（同方案、执行单 in_progress）。"""

    route_id: int = Field(gt=0, description="要绑定的 success 路线编号")


class CheckpointIn(BaseModel):
    """一条现场 checkpoint：已通过路段及其 UTC 到达时刻。"""

    checkpoint_id: str = Field(min_length=1, max_length=64, description="checkpoint 唯一编号（幂等键）")
    sequence: int = Field(ge=1, description="连续序号，从 1 开始逐条递增")
    segment_id: str = Field(min_length=1, max_length=64, description="已通过的路段编号（须为当前待行路段）")
    arrived_at: datetime = Field(description="实际到达该路段终点的 UTC 时刻（带时区，整分钟）")

    @field_validator("arrived_at")
    @classmethod
    def _arrived_aware(cls, v: datetime) -> datetime:
        return _aware_utc(v)


class CheckpointBatchIn(BaseModel):
    """一批 checkpoint：同一事务内按序应用，任一违规则整批回滚。"""

    checkpoints: list[CheckpointIn] = Field(min_length=1, description="checkpoint 批次")


class CompletedCheckpointOut(BaseModel):
    """已完成段（实际通过记录）。"""

    checkpoint_id: str
    sequence: int
    segment_id: str
    from_node: str
    to_node: str
    planned_departure_at: datetime = Field(description="上报时该段计划进入时刻（UTC）")
    planned_arrival_at: datetime = Field(description="上报时该段计划到达时刻（UTC）")
    arrived_at: datetime = Field(description="实际到达时刻（UTC）")
    drive_minutes: int
    delay_minutes: int = Field(description="实际到达 - 计划到达（分钟，负为提前）")


class RemainingStepOut(BaseModel):
    """调整后的剩余计划逐段时刻（结构与路线编排 steps 一致）。"""

    segment_id: str
    from_node: str
    to_node: str
    departure_at: datetime
    arrival_at: datetime
    drive_minutes: int
    wait_before_minutes: int


class RemainingWaitOut(BaseModel):
    """剩余计划中的节点等待记录（结构与路线编排 waits 一致）。"""

    node_id: str
    for_segment_id: str
    start_at: datetime
    end_at: datetime
    wait_minutes: int
    blocking_windows: list[RestrictionWindowOut]


class TransportProgressOut(BaseModel):
    """运输进度完整视图：绑定信息、已完成段、调整后的剩余计划与延误。"""

    progress_id: int
    execution_id: int
    route_id: int
    plan_id: int
    status: str = Field(description="in_transit / blocked（重排无解，不提交该批）/ arrived（抵达终点）")
    origin: str
    destination: str
    departure_at: datetime = Field(description="路线原始出发时刻（UTC）")
    original_arrival_at: datetime = Field(description="路线原始计划到达时刻（UTC）")
    current_node: str = Field(description="车辆当前所在节点（最后通过段的 to_node）")
    planned_arrival_at: Optional[datetime] = Field(
        default=None, description="按当前剩余计划预计到达终点时刻（UTC）；blocked 时为 null"
    )
    delay_minutes: int = Field(
        description="预计到达相对原始计划的延误（分钟，负为提前；blocked 时为最近一次延误）"
    )
    hazard_classes: list[str]
    last_sequence: int
    completed: list[CompletedCheckpointOut] = Field(description="已完成段（按序号升序）")
    remaining_segment_ids: list[str] = Field(description="调整后剩余计划的路段 ID 序列")
    remaining_steps: list[RemainingStepOut]
    remaining_waits: list[RemainingWaitOut]
    blockages: list[RouteBlockageOut] = Field(description="status=blocked 时重排无解的阻断原因")
    created_at: datetime
    updated_at: datetime


class CheckpointBatchResult(TransportProgressOut):
    """checkpoint 批次处理结果：applied 为新应用条数，skipped 为幂等重试跳过条数。"""

    applied: int
    skipped: int

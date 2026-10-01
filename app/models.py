"""ORM 模型：规划方案、禁配规则与现场执行单。"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    """一次分舱规划的请求与结果（含不可行时的冲突诊断）。"""

    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String(16), nullable=False)  # success / infeasible / undetermined
    request_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class SegregationRule(Base):
    """危险类别级禁配规则（启动时自动初始化示例数据）。"""

    __tablename__ = "segregation_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    class_a = Column(String(16), nullable=False)
    class_b = Column(String(16), nullable=False)
    level = Column(String(16), nullable=False, default="forbidden")
    description = Column(String(255), nullable=False, default="")


class Execution(Base):
    """现场执行单：创建时冻结方案的货物与舱室分配，按扫描事件推进状态。"""

    __tablename__ = "executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False, index=True)
    # in_progress / completed（全部货物卸完后自动置为 completed）
    status = Column(String(16), nullable=False, default="in_progress")
    # 已应用的最后连续序号，从 0 开始；新事件必须为 last_sequence + 1
    last_sequence = Column(Integer, nullable=False, default=0)
    # 冻结的方案分配快照：{cargo_id: compartment_id}
    assignments_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)


class ExecutionCargo(Base):
    """执行单中每件货物的计划舱室与现场状态。"""

    __tablename__ = "execution_cargos"
    __table_args__ = (
        UniqueConstraint("execution_id", "cargo_id", name="uq_execution_cargo"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(Integer, ForeignKey("executions.id"), nullable=False, index=True)
    cargo_id = Column(String(64), nullable=False)
    planned_compartment_id = Column(String(64), nullable=False)
    # pending（未装） / loaded（在舱） / unloaded（已卸）
    status = Column(String(16), nullable=False, default="pending")


class ExecutionEvent(Base):
    """已应用的扫描事件（幂等记录：event_id 唯一，载荷随首次提交固化）。"""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("execution_id", "event_id", name="uq_execution_event_id"),
        UniqueConstraint("execution_id", "sequence", name="uq_execution_event_seq"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(Integer, ForeignKey("executions.id"), nullable=False, index=True)
    event_id = Column(String(64), nullable=False)
    sequence = Column(Integer, nullable=False)
    cargo_id = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)  # load / unload
    compartment_id = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class Route(Base):
    """危险品运输路线编排结果（含道路图快照与无可行路线时的阻断原因）。"""

    __tablename__ = "routes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False, index=True)
    # success（唯一最优路线）/ infeasible（无可行路线，blockages 给出原因）
    status = Column(String(16), nullable=False)
    request_json = Column(Text, nullable=False)  # 道路图、起终点、出发时间快照
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

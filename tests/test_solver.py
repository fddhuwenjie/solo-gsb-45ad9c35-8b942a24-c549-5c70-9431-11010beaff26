"""求解器单元测试：可行性、最少舱室、稳定性、冲突诊断、超时状态判定。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ortools.sat.python import cp_model

from app import solver as solver_mod
from app.solver import Cargo, Compartment, Incompatibility, solve_plan

_real_new_solver = solver_mod._new_solver  # 未打补丁的原始求解器工厂


class _ForcedStatusSolver:
    """包装真实 CpSolver：照常求解（保证 value/objective_value 可用），
    但返回指定状态，用于模拟 time_limit_seconds 内返回 UNKNOWN 的超时分支。"""

    def __init__(self, real: cp_model.CpSolver, forced_status):
        self._real = real
        self._forced_status = forced_status
        self.parameters = real.parameters

    def solve(self, model):
        self._real.solve(model)
        return self._forced_status

    def value(self, var):
        return self._real.value(var)

    @property
    def objective_value(self):
        return self._real.objective_value


def _patch_solver_factory(factory):
    """替换 solver 模块的求解器工厂，返回恢复函数。"""
    solver_mod._new_solver = factory
    return lambda: setattr(solver_mod, "_new_solver", _real_new_solver)


def _cargos():
    return [
        Cargo("C1", "3", 10),
        Cargo("C2", "5.1", 10),
        Cargo("C3", "3", 5),
    ]


def _comps(n=3, cap=20):
    return [Compartment(str(i + 1), cap) for i in range(n)]


def test_basic_segregation_and_min_compartments():
    # C1(类别3) 与 C2(类别5.1) 禁配，C3 与 C1 同类可同舱
    incs = [Incompatibility("C1", "C2", "类别 3 与 5.1 禁配")]
    r = solve_plan(_cargos(), _comps(), incs, {}, set())
    assert r.status == "success"
    assert r.compartments_used == 2
    assert r.assignments["C1"] != r.assignments["C2"]
    assert r.assignments["C1"] == r.assignments["C3"]


def test_deterministic_same_input_same_output():
    incs = [Incompatibility("C1", "C2", "类别 3 与 5.1 禁配")]
    results = [
        solve_plan(_cargos(), _comps(), incs, {}, set()).assignments
        for _ in range(5)
    ]
    assert all(a == results[0] for a in results)


def test_stable_low_compartment_preferred():
    incs = [Incompatibility("C1", "C2", "类别 3 与 5.1 禁配")]
    r = solve_plan(_cargos(), _comps(), incs, {}, set())
    # 小编号货物优先落入小编号舱室
    assert r.assignments["C1"] == "1"
    assert r.assignments["C3"] == "1"
    assert r.assignments["C2"] == "2"


def test_forced_assignment():
    r = solve_plan(_cargos(), _comps(), [], {"C2": "3"}, set())
    assert r.status == "success"
    assert r.assignments["C2"] == "3"


def test_forbidden_assignment():
    r = solve_plan(_cargos(), _comps(), [], {}, {("C1", "1"), ("C1", "2")})
    assert r.status == "success"
    assert r.assignments["C1"] == "3"


def test_infeasible_clique_reports_cargos_and_rules():
    # 3 种货物两两禁配，但只有 2 个舱室
    incs = [
        Incompatibility("C1", "C2", "规则R1"),
        Incompatibility("C1", "C3", "规则R2"),
        Incompatibility("C2", "C3", "规则R3"),
    ]
    r = solve_plan(_cargos(), _comps(n=2), incs, {}, set())
    assert r.status == "infeasible"
    assert r.assignments == {}
    inc_conflicts = [c for c in r.conflicts if c["type"] == "incompatibility"]
    assert inc_conflicts, "应报告禁配冲突"
    assert set(inc_conflicts[0]["cargo_ids"]) == {"C1", "C2", "C3"}
    assert "规则R1" in inc_conflicts[0]["rule"]


def test_infeasible_capacity_reports_rule():
    cargos = [Cargo("C1", "3", 100)]
    r = solve_plan(cargos, _comps(n=2, cap=20), [], {}, set())
    assert r.status == "infeasible"
    assert any(c["type"] == "capacity" and c["cargo_ids"] == ["C1"] for c in r.conflicts)


def test_infeasible_forced_same_compartment():
    incs = [Incompatibility("C1", "C2", "规则R1")]
    r = solve_plan(_cargos(), _comps(), incs, {"C1": "1", "C2": "1"}, set())
    assert r.status == "infeasible"
    assert any(
        c["type"] == "incompatibility" and set(c["cargo_ids"]) == {"C1", "C2"}
        for c in r.conflicts
    )


def test_infeasible_never_returns_partial_assignments():
    incs = [
        Incompatibility("C1", "C2", "R"),
        Incompatibility("C1", "C3", "R"),
        Incompatibility("C2", "C3", "R"),
    ]
    r = solve_plan(_cargos(), _comps(n=1), incs, {}, set())
    assert r.status == "infeasible"
    assert r.assignments == {}
    assert r.compartments_used == 0


def test_stable_order_proven_when_phase2_optimal():
    # 正常求解：两个阶段均证得最优
    incs = [Incompatibility("C1", "C2", "类别 3 与 5.1 禁配")]
    r = solve_plan(_cargos(), _comps(), incs, {}, set())
    assert r.status == "success"
    assert r.proven_optimal
    assert r.stable_order_proven


def test_phase1_unknown_returns_undetermined_not_infeasible():
    # 第一阶段在 time_limit_seconds 内返回 UNKNOWN：
    # 必须是 undetermined，assignments / conflicts 均为空，不得误判为 infeasible
    restore = _patch_solver_factory(
        lambda t: _ForcedStatusSolver(_real_new_solver(t), cp_model.UNKNOWN)
    )
    try:
        r = solve_plan(_cargos(), _comps(), [], {}, set())
    finally:
        restore()
    assert r.status == "undetermined"
    assert r.assignments == {}
    assert r.conflicts == []
    assert r.compartments_used == 0
    assert not r.proven_optimal
    assert not r.stable_order_proven


def test_phase2_unknown_falls_back_to_phase1_solution():
    # 第一阶段已有完整解，第二阶段返回 UNKNOWN：
    # 回退第一阶段解，status=success，proven_optimal=False，stable_order_proven=False
    incs = [Incompatibility("C1", "C2", "类别 3 与 5.1 禁配")]
    calls = {"n": 0}

    def factory(t):
        calls["n"] += 1
        real = _real_new_solver(t)
        if calls["n"] == 1:
            return real  # 第一阶段正常求解
        return _ForcedStatusSolver(real, cp_model.UNKNOWN)  # 第二阶段模拟超时

    restore = _patch_solver_factory(factory)
    try:
        r = solve_plan(_cargos(), _comps(), incs, {}, set())
    finally:
        restore()
    assert r.status == "success"
    assert not r.proven_optimal
    assert not r.stable_order_proven
    # 回退的第一阶段解完整且满足约束
    assert set(r.assignments) == {"C1", "C2", "C3"}
    assert r.assignments["C1"] != r.assignments["C2"]
    assert r.compartments_used == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

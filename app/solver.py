"""同车分舱规划求解器。

基于 OR-Tools CP-SAT：
1. 第一阶段最小化使用舱室数；
2. 第二阶段在舱室数固定的前提下，最小化"货物序号加权的舱室编号"，
   使小编号货物优先进入小编号舱室，配合单线程 + 固定随机种子，
   保证相同输入得到稳定一致的结果。

求解状态判定：
- 仅当 CP-SAT 证明 INFEASIBLE 时才判定不可行，通过 diagnose() 输出导致
  冲突的货物与规则，绝不返回部分方案；
- 第一阶段在时限内返回 UNKNOWN（既未找到解也未证明不可行）时返回
  undetermined：assignments 与 conflicts 均为空，不得误判为不可行；
- 第一阶段已有完整解而第二阶段返回 UNKNOWN 时，回退第一阶段完整解，
  返回 success，但 proven_optimal=False 且 stable_order_proven=False。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from ortools.sat.python import cp_model

SOLVER_SEED = 42


@dataclass(frozen=True)
class Cargo:
    cargo_id: str
    hazard_class: str
    quantity: int


@dataclass(frozen=True)
class Compartment:
    compartment_id: str
    capacity: int


@dataclass(frozen=True)
class Incompatibility:
    cargo_a: str
    cargo_b: str
    rule: str  # 规则描述，用于冲突报告


@dataclass
class SolveResult:
    status: str  # "success" / "infeasible" / "undetermined"
    proven_optimal: bool
    stable_order_proven: bool = False  # 第二阶段稳定次序是否证得最优
    assignments: dict[str, str] = field(default_factory=dict)  # cargo_id -> compartment_id
    compartments_used: int = 0
    conflicts: list[dict] = field(default_factory=list)


def _new_solver(time_limit: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    # 单线程 + 固定随机种子 => 求解过程可复现
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = SOLVER_SEED
    solver.parameters.max_time_in_seconds = time_limit
    return solver


def solve_plan(
    cargos: list[Cargo],
    compartments: list[Compartment],
    incompatibilities: list[Incompatibility],
    forced: dict[str, str],
    forbidden: set[tuple[str, str]],
    time_limit: float = 30.0,
) -> SolveResult:
    """求解分舱方案。cargos / compartments 需已由调用方按编号排序。"""
    n, m = len(cargos), len(compartments)
    if n == 0:
        return SolveResult(status="success", proven_optimal=True, stable_order_proven=True)

    model = cp_model.CpModel()
    x = {(i, j): model.new_bool_var(f"x_{i}_{j}") for i in range(n) for j in range(m)}

    # 每种货物恰好进入一个舱室
    for i in range(n):
        model.add_exactly_one(x[i, j] for j in range(m))
    # 舱室容量
    for j in range(m):
        model.add(sum(cargos[i].quantity * x[i, j] for i in range(n)) <= compartments[j].capacity)
    # 禁配隔离：同一舱室内不得同时出现禁配货物
    cidx = {c.cargo_id: i for i, c in enumerate(cargos)}
    for inc in incompatibilities:
        a, b = cidx[inc.cargo_a], cidx[inc.cargo_b]
        for j in range(m):
            model.add(x[a, j] + x[b, j] <= 1)
    # 指定舱室条件
    kidx = {c.compartment_id: j for j, c in enumerate(compartments)}
    for cargo_id, comp_id in forced.items():
        model.add(x[cidx[cargo_id], kidx[comp_id]] == 1)
    for cargo_id, comp_id in forbidden:
        model.add(x[cidx[cargo_id], kidx[comp_id]] == 0)
    # 舱室使用标记
    used = [model.new_bool_var(f"used_{j}") for j in range(m)]
    for j in range(m):
        for i in range(n):
            model.add(x[i, j] <= used[j])

    # 第一阶段：最少舱室数
    model.minimize(sum(used))
    solver = _new_solver(time_limit)
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE:
        # 只有 CP-SAT 证明不可行，才诊断并报告冲突
        return SolveResult(
            status="infeasible",
            proven_optimal=False,
            conflicts=diagnose(cargos, compartments, incompatibilities, forced, forbidden),
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # UNKNOWN（时限内未得出结论）：既未找到解也未证明不可行，
        # 不得误判为不可行，assignments 与 conflicts 均保持为空
        return SolveResult(status="undetermined", proven_optimal=False)
    proven = status == cp_model.OPTIMAL
    best_k = int(round(solver.objective_value))
    # 留存第一阶段完整解，作为第二阶段超时时的回退方案
    fallback = {
        cargos[i].cargo_id: compartments[j].compartment_id
        for i in range(n)
        for j in range(m)
        if solver.value(x[i, j])
    }

    # 第二阶段：舱室数固定，按舱室编号 + 货物编号稳定决策
    model.add(sum(used) == best_k)
    weights = [n - i for i in range(n)]  # 货物编号越小权重越大，优先落入小编号舱室
    model.minimize(sum(x[i, j] * j * weights[i] for i in range(n) for j in range(m)))
    solver2 = _new_solver(time_limit)
    status2 = solver2.solve(model)
    if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        stable_proven = status2 == cp_model.OPTIMAL
        assignments = {
            cargos[i].cargo_id: compartments[j].compartment_id
            for i in range(n)
            for j in range(m)
            if solver2.value(x[i, j])
        }
        proven = proven and stable_proven
    else:
        # 第二阶段超时（UNKNOWN）：回退第一阶段完整解。
        # 第一阶段解必然满足新增的舱室数约束，故 INFEASIBLE 理论上不会出现，
        # 一并按回退处理；稳定次序未获证明，整体也不算证得最优。
        assignments = fallback
        stable_proven = False
        proven = False

    return SolveResult(
        status="success",
        proven_optimal=proven,
        stable_order_proven=stable_proven,
        assignments=assignments,
        compartments_used=best_k,
    )


# ---------------------------------------------------------------------------
# 不可行诊断
# ---------------------------------------------------------------------------

def diagnose(
    cargos: list[Cargo],
    compartments: list[Compartment],
    incompatibilities: list[Incompatibility],
    forced: dict[str, str],
    forbidden: set[tuple[str, str]],
) -> list[dict]:
    """分析不可行原因，返回冲突列表（货物 + 规则）。"""
    conflicts: list[dict] = []
    cargo_by_id = {c.cargo_id: c for c in cargos}
    caps = {c.compartment_id: c.capacity for c in compartments}
    m = len(compartments)

    if m == 0:
        return [{
            "type": "capacity",
            "cargo_ids": sorted(cargo_by_id),
            "rule": "舱室容量限制",
            "detail": "未提供任何可用舱室",
        }]

    # 1) 单舱容量：货物超过最大舱室容量
    max_cap = max(caps.values())
    for c in cargos:
        if c.quantity > max_cap:
            conflicts.append({
                "type": "capacity",
                "cargo_ids": [c.cargo_id],
                "rule": "舱室容量限制",
                "detail": f"货物 {c.cargo_id} 数量 {c.quantity} 超过最大舱室容量 {max_cap}",
            })

    # 2) 总容量
    total_q = sum(c.quantity for c in cargos)
    total_cap = sum(caps.values())
    if total_q > total_cap:
        conflicts.append({
            "type": "capacity",
            "cargo_ids": sorted(cargo_by_id),
            "rule": "舱室总容量限制",
            "detail": f"货物总量 {total_q} 超过舱室总容量 {total_cap}",
        })

    # 3) 指定舱室条件自身矛盾 / 指定舱室容量超限
    for cargo_id, comp_id in sorted(forced.items()):
        if (cargo_id, comp_id) in forbidden:
            conflicts.append({
                "type": "assignment",
                "cargo_ids": [cargo_id],
                "rule": "指定舱室条件",
                "detail": f"货物 {cargo_id} 同时被强制放入且禁止放入舱室 {comp_id}",
            })
    forced_load: dict[str, list[str]] = {}
    for cargo_id, comp_id in forced.items():
        forced_load.setdefault(comp_id, []).append(cargo_id)
    for comp_id, cids in sorted(forced_load.items()):
        load = sum(cargo_by_id[c].quantity for c in cids)
        if comp_id in caps and load > caps[comp_id]:
            conflicts.append({
                "type": "assignment",
                "cargo_ids": sorted(cids),
                "rule": "指定舱室条件 + 舱室容量限制",
                "detail": f"指定到舱室 {comp_id} 的货物总量 {load} 超过其容量 {caps[comp_id]}",
            })

    # 禁配规则索引：货物对 -> 规则描述
    inc_map: dict[tuple[str, str], list[str]] = {}
    for inc in incompatibilities:
        key = tuple(sorted((inc.cargo_a, inc.cargo_b)))
        inc_map.setdefault(key, [])
        if inc.rule not in inc_map[key]:
            inc_map[key].append(inc.rule)

    # 4) 禁配货物被指定到同一舱室
    for (a, b), rules in sorted(inc_map.items()):
        if forced.get(a) is not None and forced.get(a) == forced.get(b):
            conflicts.append({
                "type": "incompatibility",
                "cargo_ids": [a, b],
                "rule": "; ".join(rules),
                "detail": f"禁配货物 {a}、{b} 被指定条件强制放入同一舱室 {forced[a]}",
            })

    # 5) 货物无可用舱室（容量本可放入，但被禁止条件全部排除）
    for c in cargos:
        fit_by_cap = [comp for comp in compartments if c.quantity <= comp.capacity]
        if fit_by_cap and all((c.cargo_id, comp.compartment_id) in forbidden for comp in fit_by_cap):
            conflicts.append({
                "type": "assignment",
                "cargo_ids": [c.cargo_id],
                "rule": "指定舱室条件",
                "detail": f"货物 {c.cargo_id} 被禁止条件排除了所有可容纳它的舱室",
            })

    # 6) 禁配团（clique）：k 种货物两两禁配，至少需要 k 个舱室
    adj: dict[str, set[str]] = {c.cargo_id: set() for c in cargos}
    for a, b in inc_map:
        adj[a].add(b)
        adj[b].add(a)
    clique = _max_clique(sorted(adj), adj)
    if len(clique) > m:
        rules = sorted({
            rule
            for a, b in combinations(clique, 2)
            for rule in inc_map.get(tuple(sorted((a, b))), [])
        })
        conflicts.append({
            "type": "incompatibility",
            "cargo_ids": clique,
            "rule": "; ".join(rules),
            "detail": f"{len(clique)} 种货物两两禁配，至少需要 {len(clique)} 个舱室，但仅有 {m} 个",
        })

    # 7) 兜底：以上检查均未命中时，区分"隔离图不可染色"与"容量+禁配组合冲突"
    if not conflicts:
        all_pairs = "; ".join(
            f"{a}-{b}: {'/'.join(rules)}" for (a, b), rules in sorted(inc_map.items())
        )
        if inc_map and not _colorable(sorted(adj), adj, m):
            conflicts.append({
                "type": "incompatibility",
                "cargo_ids": sorted(cargo_by_id),
                "rule": all_pairs,
                "detail": f"禁配关系构成的隔离图无法用 {m} 个舱室完成分离（不可 {m} 染色）",
            })
        else:
            conflicts.append({
                "type": "mixed",
                "cargo_ids": sorted(cargo_by_id),
                "rule": all_pairs or "舱室容量限制",
                "detail": "容量限制与禁配/指定条件共同导致无可行方案，请增加舱室容量或减少货物",
            })
    return conflicts


def _max_clique(nodes: list[str], adj: dict[str, set[str]]) -> list[str]:
    """Bron–Kerbosch（带枢轴）求最大团，结果按编号排序保证确定性。"""
    best: list[str] = []

    def bk(r: set[str], p: set[str], x: set[str]) -> None:
        nonlocal best
        if not p and not x:
            cand = sorted(r)
            if len(cand) > len(best) or (len(cand) == len(best) and cand < best):
                best = cand
            return
        if len(r) + len(p) < len(best):
            return
        pivot = max(p | x, key=lambda u: len(adj[u] & p), default=None)
        candidates = sorted(p - (adj[pivot] if pivot is not None else set()))
        for v in candidates:
            bk(r | {v}, p & adj[v], x & adj[v])
            p = p - {v}
            x = x | {v}

    bk(set(), set(nodes), set())
    return best


def _colorable(nodes: list[str], adj: dict[str, set[str]], m: int) -> bool:
    """判断禁配隔离图是否可用 m 个舱室染色（分离）。"""
    if m <= 0:
        return not nodes
    model = cp_model.CpModel()
    color = {v: model.new_int_var(0, m - 1, f"c_{v}") for v in nodes}
    for u in nodes:
        for v in adj[u]:
            if u < v:
                model.add(color[u] != color[v])
    solver = _new_solver(5.0)
    return solver.solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)

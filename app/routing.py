"""危险品运输路线编排：带禁行窗口与节点等待的时间相关多标号最短路。

输入为本地有向道路图：
- 路段：行驶分钟、禁运危险类别（恒闭）、UTC 禁行窗口 [start, end)（半开）；
- 节点：是否允许等待。

通行规则：
- 车辆在时刻 t 进入行驶时长 d 的路段，通行区间为 [t, t+d)；
  与任一禁行窗口 [s, e) 相交（t < e 且 t+d > s）即不可进入；
  t == e（窗口结束瞬间进入）或 t+d == s（窗口开始瞬间到达）均允许；
- 节点允许等待时，等待到该路段下一可行时刻再进入；不允许等待则该走法不可行；
- 禁运类别为恒闭限制，等待不能解除。

环行（重复经过同一路段）是允许的：在**所有节点均禁止等待**的图中，车辆无法
原地等待，只能通过连续环行把到达时刻推移到窗口结束之后；"含环路径删环后首达
不晚"仅在可等待时成立，故不能用简单边路径限制搜索空间。

有限终止：取全部窗口最晚结束时刻 H（无窗口时 H=0）。时刻 t >= H 后所有窗口
均已结束，路段可行性不再随时间变化（仅余恒闭的类别禁运），问题退化为静态图：
- 动态搜索只扩展 t < H 的标号，每次移动 t 严格增加（行驶分钟为正整数），
  故搜索深度有限；配合下列支配规则，每个节点保留的标号数同样以 H 为上界；
- 任一标号在 t >= H 到达某节点后，直接用预计算的静态后缀补全：非禁运边上的
  最短行驶路径，等长时取路段 ID 序列字典序最小者（后缀无等待，最短行驶即
  最早到达）。

唯一结果按字典序目标确定：最早到达 → 总等待最少 → 路段 ID 序列字典序。
由于"在禁行窗口前等待"会抹平不同走法的到达时间差（更长的行驶路径可能用更少
等待追平到达时刻），同一节点需要保留多个互不支配的标号。标号状态为
(到达时刻 t, 累计行驶 drive)，t == drive + 总等待；对任意后续路段序列：

- 到达时刻是关于 t 的非递减函数（FIFO），t 更小者不会更晚到达；
- 被窗口"抹平"到达时刻时，drive 更大者总等待更少；

支配判定分两种节点（边的可行性只取决于节点与进入时刻，与历史路径无关，
故不再要求已用边集合包含——这正是允许环行的关键）：

- **允许等待**的节点：早到标号可等待到晚到标号的时刻，故 A 支配 B 当且仅当
  t_A < t_B 且 drive_A **严格大于** drive_B（drive 相等时等待差会被追平，
  到达与等待都相同，必须靠路段序列字典序裁决，双方都保留）；
- **禁止等待**的节点：t 不同的标号不可比——晚到标号可能恰在窗口结束后通过，
  而早到标号被窗口关闭且无法等待。

两类节点共有的安全规则：t 相同则 drive 更大者支配（后续每段等待完全相同，
到达相同且等待更少）；相同 `(t, drive)` 只保留路段序列字典序最小者
（追加共同后缀不改变字典序；且两条同节点同 `(t, drive)` 的路径不可能互为
前缀——多出的环必含正的行驶分钟，不可能同时刻同 drive 回到同一节点）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import heapq


@dataclass(frozen=True)
class Window:
    """禁行窗口 [start, end)，时刻为相对出发时刻的分钟偏移。"""

    start: int
    end: int


@dataclass(frozen=True)
class RoadNode:
    node_id: str
    allow_waiting: bool


@dataclass(frozen=True)
class RoadSegment:
    segment_id: str
    from_node: str
    to_node: str
    drive_minutes: int
    embargo_classes: frozenset[str]
    windows: tuple[Window, ...]  # 已按 start 升序排列


@dataclass(frozen=True)
class Step:
    """逐段时刻记录。"""

    segment_id: str
    from_node: str
    to_node: str
    departure: int  # 进入路段的时刻（相对出发的分钟偏移）
    arrival: int  # 到达下游节点的时刻
    drive_minutes: int
    wait_before_minutes: int  # 在上游节点为本段等待的分钟数


@dataclass(frozen=True)
class Wait:
    """节点等待记录。"""

    node_id: str
    for_segment_id: str
    start: int  # 到达节点（开始等待）的时刻
    end: int  # 实际驶离的时刻
    wait_minutes: int
    blocking_windows: tuple[Window, ...]  # 导致等待的禁行窗口（按开始时刻升序）


@dataclass(frozen=True)
class EmbargoBlockage:
    """阻断原因：路段对本车载货类别恒闭。"""

    type: str
    node_id: str
    segment_id: str
    to_node: str
    embargo_classes: tuple[str, ...]  # 该路段全部禁运类别
    matched_classes: tuple[str, ...]  # 与本车载货类别相交的部分


@dataclass(frozen=True)
class NoWaitingBlockage:
    """阻断原因：最早到达时路段被窗口关闭，而节点不允许等待。"""

    type: str
    node_id: str
    segment_id: str
    to_node: str
    arrival: int
    drive_minutes: int
    windows: tuple[Window, ...]  # 该到达时刻下与通行区间相交的窗口


@dataclass(frozen=True)
class UnreachableBlockage:
    """兜底阻断原因：起止点之间不存在任何可行路线。"""

    type: str
    origin: str
    destination: str


@dataclass
class RoutePlan:
    status: str  # success / infeasible
    steps: list[Step] = field(default_factory=list)
    waits: list[Wait] = field(default_factory=list)
    arrival: int | None = None
    total_drive: int = 0
    total_wait: int = 0
    blockages: list = field(default_factory=list)


@dataclass(frozen=True)
class _Label:
    """搜索标号：节点、到达时刻、累计行驶/等待分钟、完整路径与记录。"""

    node: str
    t: int
    drive: int
    total_wait: int
    path: tuple[str, ...]
    steps: tuple[Step, ...]
    waits: tuple[Wait, ...]


def earliest_entry(
    t: int, drive: int, windows: tuple[Window, ...], allow_waiting: bool
) -> tuple[int | None, int, tuple[Window, ...]]:
    """计算在时刻 t 进入时长 drive 的路段的最早可行进入时刻。

    返回 (进入时刻, 等待分钟, 导致等待的窗口)；不允许等待且当前不可进入时
    进入时刻为 None，第三个返回值为当前与通行区间相交的窗口。
    """
    cand = t
    forced: list[Window] = []
    seen: set[Window] = set()
    while True:
        # [cand, cand+drive) 与 [w.start, w.end) 相交
        hits = [w for w in windows if cand < w.end and cand + drive > w.start]
        if not hits:
            return cand, cand - t, tuple(forced)
        if not allow_waiting:
            return None, 0, tuple(sorted(hits, key=lambda w: (w.start, w.end)))
        for w in hits:
            if w not in seen:
                seen.add(w)
                forced.append(w)
        # 跳到所有相交窗口中最晚的结束时刻（相邻/重叠窗口可能需要多跳）
        cand = max(cand, max(w.end for w in hits))


def _static_shortest(
    adj: dict[str, list[RoadSegment]],
    node_ids: set[str],
    destination: str,
    carried_classes: set[str],
) -> tuple[dict[str, int], dict[str, RoadSegment]]:
    """t >= H（窗口全部失效）后的静态后缀：仅非禁运边。

    返回 (最短行驶分钟 dist, 字典序最小最短路径的首条边选择)。
    在终点反向的 Dijkstra 求 dist；对每个节点取使
    (drive + dist[to], segment_id) 最小的出边——最短路径的后缀仍须最短，
    而整条路径的字典序由首条路段 ID 决定（路段 ID 唯一，不会同 ID 并列）。
    所选边满足 dist 严格递减（drive >= 1），故沿选择链不可能成环。
    """
    inf = 10**18
    dist = {u: inf for u in node_ids}
    dist[destination] = 0
    radj: dict[str, list[RoadSegment]] = {}
    for outs in adj.values():
        for seg in outs:
            if seg.embargo_classes & carried_classes:
                continue
            radj.setdefault(seg.to_node, []).append(seg)

    heap: list[tuple[int, str]] = [(0, destination)]
    while heap:
        d, u = heapq.heappop(heap)
        if d != dist[u]:
            continue
        for seg in radj.get(u, ()):
            nd = d + seg.drive_minutes
            if nd < dist[seg.from_node]:
                dist[seg.from_node] = nd
                heapq.heappush(heap, (nd, seg.from_node))

    choice: dict[str, RoadSegment] = {}
    for u in node_ids:
        if u == destination or dist[u] == inf:
            continue
        best_key = None
        for seg in adj.get(u, ()):
            if seg.embargo_classes & carried_classes or dist[seg.to_node] == inf:
                continue
            key = (seg.drive_minutes + dist[seg.to_node], seg.segment_id)
            if best_key is None or key < best_key:
                best_key = key
                choice[u] = seg
    return dist, choice


def plan_route(
    segments: list[RoadSegment],
    nodes: list[RoadNode],
    origin: str,
    destination: str,
    carried_classes: set[str],
) -> RoutePlan:
    """编排唯一最优路线；不可行时返回稳定排序的阻断原因且不含任何行程段。"""
    if origin == destination:
        return RoutePlan(status="success", arrival=0)  # 零段行程：到达时刻即出发时刻

    adj: dict[str, list[RoadSegment]] = {}
    for seg in segments:
        adj.setdefault(seg.from_node, []).append(seg)
    for outs in adj.values():
        outs.sort(key=lambda s: s.segment_id)  # 放松顺序确定，保证记录可复现
    allow_wait = {n.node_id: n.allow_waiting for n in nodes}
    node_ids = {n.node_id for n in nodes}
    for seg in segments:
        node_ids.add(seg.from_node)
        node_ids.add(seg.to_node)

    # 有限终止界：此后所有禁行窗口都已结束，可行性不再随时间变化
    horizon = max((w.end for seg in segments for w in seg.windows), default=0)
    dist, choice = _static_shortest(adj, node_ids, destination, carried_classes)

    root = _Label(node=origin, t=0, drive=0, total_wait=0, path=(), steps=(), waits=())
    buckets: dict[str, list[_Label]] = {}
    heap: list[tuple[int, int, _Label]] = []
    counter = 0
    best_dest: _Label | None = None

    # 阻断原因收集（同一“节点 × 路段”只记一条；no_waiting 取最早到达尝试）
    nowait_hits: dict[tuple[str, str], NoWaitingBlockage] = {}
    # 动态搜索中至少被一个标号成功通过的边
    passed_edges: set[tuple[str, str]] = set()
    # 实际收到过标号的节点（动态区 t < H）与在 t >= H 经可行前缀到达的节点/种子
    dyn_reached: set[str] = set()
    late_seeds: set[str] = set()

    def complete_static(label: _Label) -> _Label | None:
        """用预计算的静态（最短行驶、字典序最小）后缀把标号补到终点。"""
        u = label.node
        if dist[u] >= 10**18:
            return None
        t, drive = label.t, label.drive
        path, steps = label.path, label.steps
        while u != destination:
            seg = choice[u]
            steps = steps + (Step(
                segment_id=seg.segment_id,
                from_node=u,
                to_node=seg.to_node,
                departure=t,
                arrival=t + seg.drive_minutes,
                drive_minutes=seg.drive_minutes,
                wait_before_minutes=0,  # 静态区无窗口，无需等待
            ),)
            path = path + (seg.segment_id,)
            t += seg.drive_minutes
            drive += seg.drive_minutes
            u = seg.to_node
        return _Label(
            node=destination, t=t, drive=drive, total_wait=label.total_wait,
            path=path, steps=steps, waits=label.waits,
        )

    def consider(label: _Label) -> None:
        """标号落地分流：终点裁决 / t>=H 静态补全 / 进入动态桶。"""
        nonlocal best_dest
        if label.node == destination:
            if best_dest is None or (label.t, label.total_wait, label.path) < (
                best_dest.t, best_dest.total_wait, best_dest.path
            ):
                best_dest = label
            return
        if label.t >= horizon:
            # 窗口全部失效：静态后缀即该标号能取得的最优延续
            late_seeds.add(label.node)
            cand = complete_static(label)
            if cand is not None:
                consider(cand)
            return
        insert(label)

    def insert(label: _Label) -> None:
        nonlocal counter
        # 到达终点不可能比已知最优到达“更早”（后续行驶分钟为正），故仅严格更晚才丢弃；
        # t 相同的标号必须保留——总等待可能更少、路段序列可能字典序更小
        if best_dest is not None and label.t > best_dest.t:
            return
        # 禁等节点上“更早到达”不能等待到晚到时刻，晚到标号可能恰好越过窗口，
        # 故 t 更小的标号不具支配力；仅相同 t 可比（边可行性只取决于节点与 t）。
        waiting_node = allow_wait.get(label.node, False)
        bucket = buckets.setdefault(label.node, [])
        survivors: list[_Label] = []
        for old in bucket:
            if old.t == label.t and old.drive == label.drive:
                # 相同状态：只保留路段序列字典序最小者
                if label.path < old.path:
                    continue  # old 被替换
                return
            # 新标号被旧标号支配
            if old.t == label.t and old.drive > label.drive:
                return
            if waiting_node and old.t < label.t and old.drive > label.drive:
                return
            # 旧标号被新标号支配
            if old.t == label.t and label.drive > old.drive:
                continue
            if waiting_node and label.t < old.t and label.drive > old.drive:
                continue
            survivors.append(old)
        survivors.append(label)
        buckets[label.node] = survivors
        dyn_reached.add(label.node)
        heapq.heappush(heap, (label.t, counter, label))
        counter += 1

    consider(root)

    while heap:
        _, _, label = heapq.heappop(heap)
        # 可能已在出堆前被更优标号淘汰
        if not any(l is label for l in buckets.get(label.node, ())):
            continue
        if best_dest is not None and label.t > best_dest.t:
            continue

        for seg in adj.get(label.node, ()):
            key = (label.node, seg.segment_id)
            matched = seg.embargo_classes & carried_classes
            if matched:
                # 禁运类别为恒闭阻断，与到达时刻无关；阻断原因在搜索后统一收集
                continue

            dep, wait_min, forced = earliest_entry(
                label.t, seg.drive_minutes, seg.windows, allow_wait[label.node]
            )
            if dep is None:
                # 多标号按到达时刻出堆，首条即最早到达尝试，冻结为候选阻断原因；
                # 但若存在另一标号成功通过该边（含 t>=H 静态可行），最终会被剔除
                nowait_hits.setdefault(
                    key,
                    NoWaitingBlockage(
                        type="no_waiting",
                        node_id=label.node,
                        segment_id=seg.segment_id,
                        to_node=seg.to_node,
                        arrival=label.t,
                        drive_minutes=seg.drive_minutes,
                        windows=forced,
                    ),
                )
                continue
            passed_edges.add(key)

            step = Step(
                segment_id=seg.segment_id,
                from_node=label.node,
                to_node=seg.to_node,
                departure=dep,
                arrival=dep + seg.drive_minutes,
                drive_minutes=seg.drive_minutes,
                wait_before_minutes=wait_min,
            )
            waits = label.waits
            if wait_min > 0:
                waits = waits + (Wait(
                    node_id=label.node,
                    for_segment_id=seg.segment_id,
                    start=label.t,
                    end=dep,
                    wait_minutes=wait_min,
                    blocking_windows=tuple(sorted(forced, key=lambda w: (w.start, w.end))),
                ),)
            consider(_Label(
                node=seg.to_node,
                t=dep + seg.drive_minutes,
                drive=label.drive + seg.drive_minutes,
                total_wait=label.total_wait + wait_min,
                path=label.path + (seg.segment_id,),
                steps=label.steps + (step,),
                waits=waits,
            ))

    if best_dest is not None:
        return RoutePlan(
            status="success",
            steps=list(best_dest.steps),
            waits=list(best_dest.waits),
            arrival=best_dest.t,
            total_drive=best_dest.drive,
            total_wait=best_dest.total_wait,
        )

    # 不可行：阻断原因稳定排序（类型 → 节点 → 路段），绝不返回部分行程。
    # t>=H 的种子经非禁运边可达的节点：这些边在窗口失效后确实可通行，
    # 对应 no_waiting 候选应剔除（“另一标号成功通过该边”）。
    late_reached = set(late_seeds)
    stack = list(late_seeds)
    while stack:
        u = stack.pop()
        for seg in adj.get(u, ()):
            if seg.embargo_classes & carried_classes:
                continue
            passed_edges.add((u, seg.segment_id))
            if seg.to_node not in late_reached:
                late_reached.add(seg.to_node)
                stack.append(seg.to_node)

    # 类别禁运阻断：从任何实际可达节点出发的恒闭边都稳定报出
    embargo_blockages: list[EmbargoBlockage] = []
    for u in sorted(dyn_reached | late_reached):
        for seg in adj.get(u, ()):
            matched = seg.embargo_classes & carried_classes
            if not matched:
                continue
            embargo_blockages.append(EmbargoBlockage(
                type="embargo",
                node_id=u,
                segment_id=seg.segment_id,
                to_node=seg.to_node,
                embargo_classes=tuple(sorted(seg.embargo_classes)),
                matched_classes=tuple(sorted(matched)),
            ))

    type_rank = {"embargo": 0, "no_waiting": 1}
    blockages: list = sorted(
        [*embargo_blockages,
         *(b for k, b in nowait_hits.items() if k not in passed_edges)],
        key=lambda b: (type_rank[b.type], b.node_id, b.segment_id),
    )
    if not blockages:
        blockages.append(UnreachableBlockage(
            type="unreachable", origin=origin, destination=destination
        ))
    return RoutePlan(status="infeasible", blockages=blockages)

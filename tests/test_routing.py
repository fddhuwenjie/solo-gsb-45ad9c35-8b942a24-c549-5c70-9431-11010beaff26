"""路线编排算法测试：禁运、半开窗口、节点等待、三级裁决、阻断原因稳定性。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routing import RoadNode, RoadSegment, RoutePlan, Window, earliest_entry, plan_route


def seg(sid, u, v, d, embargo=(), windows=()):
    return RoadSegment(
        segment_id=sid, from_node=u, to_node=v, drive_minutes=d,
        embargo_classes=frozenset(embargo),
        windows=tuple(Window(s, e) for s, e in windows),
    )


def nodes(wait):
    return [RoadNode(nid, allow) for nid, allow in wait.items()]


def test_direct_route_no_restriction():
    r = plan_route(
        [seg("r1", "A", "B", 10)], nodes({"A": True, "B": False}),
        "A", "B", set(),
    )
    assert r.status == "success"
    assert r.arrival == 10
    assert [s.segment_id for s in r.steps] == ["r1"]
    assert r.waits == [] and r.total_wait == 0


def test_origin_equals_destination_is_zero_segment_success():
    r = plan_route([], nodes({"A": True}), "A", "A", set())
    assert r.status == "success"
    assert r.steps == [] and r.arrival == 0


def test_embargo_class_blocks_segment():
    r = plan_route(
        [seg("r1", "A", "B", 10, embargo={"3"})], nodes({"A": True, "B": False}),
        "A", "B", {"3"},
    )
    assert r.status == "infeasible"
    assert r.steps == [] and r.waits == []  # 不得给出部分行程
    assert len(r.blockages) == 1
    b = r.blockages[0]
    assert b.type == "embargo" and b.segment_id == "r1"
    assert b.matched_classes == ("3",)
    # 不载货匹配类别时不阻断
    r2 = plan_route(
        [seg("r1", "A", "B", 10, embargo={"3"})], nodes({"A": True, "B": False}),
        "A", "B", {"5.1"},
    )
    assert r2.status == "success"


def test_half_open_window_boundaries():
    # 窗口 [10,20)，行驶 10 分钟：
    # t=0 的通行区间 [0,10) 右端点恰为窗口开始 => 半开区间不相交，直接可行；
    # t=1 的 [1,11) 相交且节点禁等 => 阻断。
    r = plan_route(
        [seg("r1", "A", "B", 10, windows=[(10, 20)])],
        nodes({"A": False, "B": False}), "A", "B", set(),
    )
    assert r.status == "success"
    assert r.arrival == 10 and r.total_wait == 0


def test_earliest_entry_intersection_semantics():
    w = (Window(10, 20),)
    # [0,10) 与 [10,20) 不相交（右端点开）
    assert earliest_entry(0, 10, w, allow_waiting=False)[0] == 0
    # [5,15) 与窗口相交，不允许等待 => None
    dep, _, hits = earliest_entry(5, 10, w, allow_waiting=False)
    assert dep is None and hits == w
    # 允许等待 => 跳到 20，[20,30) 与窗口不相交
    dep, wait_min, forced = earliest_entry(5, 10, w, allow_waiting=True)
    assert (dep, wait_min) == (20, 15) and forced == w
    # t == 10 进入 [10,20) 完全落在窗口内 => 等到 20
    assert earliest_entry(10, 10, w, True)[:2] == (20, 10)


def test_wait_at_node_until_window_passes():
    # A--r1(10)-->B，窗口 [0,15)；A 允许等待，等待到 15 进入，25 到达
    r = plan_route(
        [seg("r1", "A", "B", 10, windows=[(0, 15)])],
        nodes({"A": True, "B": False}), "A", "B", set(),
    )
    assert r.status == "success"
    assert r.arrival == 25 and r.total_wait == 15
    assert len(r.waits) == 1
    wt = r.waits[0]
    assert (wt.node_id, wt.start, wt.end, wt.wait_minutes, wt.for_segment_id) == (
        "A", 0, 15, 15, "r1"
    )
    assert [w.start for w in wt.blocking_windows] == [0]
    st = r.steps[0]
    assert (st.departure, st.arrival, st.wait_before_minutes) == (15, 25, 15)


def test_no_waiting_node_reports_blockage():
    r = plan_route(
        [seg("r1", "A", "B", 10, windows=[(0, 15)])],
        nodes({"A": False, "B": False}), "A", "B", set(),
    )
    assert r.status == "infeasible"
    assert r.steps == []
    assert len(r.blockages) == 1
    b = r.blockages[0]
    assert b.type == "no_waiting"
    assert (b.node_id, b.segment_id, b.arrival) == ("A", "r1", 0)
    assert [w.start for w in b.windows] == [0]


def test_window_half_open_end_allows_exact_entry():
    # 窗口 [5,20)：t=20 进入可行，无需等待
    w = (Window(5, 20),)
    assert earliest_entry(20, 10, w, False) == (20, 0, ())


def test_multiple_overlapping_windows_jump_to_last_end():
    # [5,10), [10,20) 相邻合并效果：t=5 进入需穿越两段 => 等到 20
    w = (Window(5, 10), Window(10, 20))
    dep, wait_min, forced = earliest_entry(5, 10, w, True)
    assert (dep, wait_min) == (20, 15)
    assert len(forced) == 2


def test_earliest_arrival_preferred():
    # r1 直达 10 分钟；r2+r3 绕行 6+6=12 分钟
    segments = [
        seg("r1", "A", "Z", 10),
        seg("r2", "A", "M", 6),
        seg("r3", "M", "Z", 6),
    ]
    r = plan_route(segments, nodes({"A": False, "M": False, "Z": False}), "A", "Z", set())
    assert r.status == "success"
    assert r.arrival == 10 and r.total_wait == 0
    assert [s.segment_id for s in r.steps] == ["r1"]


def test_equal_arrival_prefers_less_wait():
    # 直达 r1=20 但被窗口 [0,10) 拦在起点：等待 10，30 到达；
    # 绕行 r2(10)+r3(10)=20 行驶、无窗口：20 到达 => 绕行严格更早
    segments = [
        seg("r1", "A", "Z", 20, windows=[(0, 10)]),
        seg("r2", "A", "M", 10),
        seg("r3", "M", "Z", 10),
    ]
    r = plan_route(segments, nodes({"A": True, "M": True, "Z": False}), "A", "Z", set())
    assert r.arrival == 20 and r.total_wait == 0
    assert [s.segment_id for s in r.steps] == ["r2", "r3"]


def test_wait_pooling_tie_broken_by_wait_then_lexicographic():
    # 构造到达时刻相同的两条路径：
    # r1 直达 drive=20，窗口 [0,10) 在起点等待 10，到达 30（等待10）；
    # r2+r3 绕行 drive=30 无窗口，30 到达（等待0）=> 等待更少者胜。
    segments = [
        seg("r1", "A", "Z", 20, windows=[(0, 10)]),
        seg("r2", "A", "M", 15),
        seg("r3", "M", "Z", 15),
    ]
    r = plan_route(segments, nodes({"A": True, "M": False, "Z": False}), "A", "Z", set())
    assert r.arrival == 30
    assert r.total_wait == 0
    assert [s.segment_id for s in r.steps] == ["r2", "r3"]


def test_lexicographic_segment_sequence_on_full_tie():
    # 两条路径行驶与等待完全相同：字典序 a-* 胜出
    segments = [
        seg("a1", "A", "M", 5),
        seg("a2", "M", "Z", 5),
        seg("b1", "A", "N", 5),
        seg("b2", "N", "Z", 5),
    ]
    r = plan_route(segments, nodes({"A": False, "M": False, "N": False, "Z": False}),
                   "A", "Z", set())
    assert r.arrival == 10
    assert [s.segment_id for s in r.steps] == ["a1", "a2"]


def test_wait_at_intermediate_node_when_origin_cannot_help():
    # 起点 A 禁止等待；直达在出发即闭；
    # 绕行到 W（允许等待），W->Z 路段有窗口，在 W 等待后通过。
    segments = [
        seg("direct", "A", "Z", 10, windows=[(0, 30)]),
        seg("toW", "A", "W", 5),
        seg("wtoZ", "W", "Z", 10, windows=[(0, 20)]),
    ]
    r = plan_route(segments, nodes({"A": False, "W": True, "Z": False}), "A", "Z", set())
    assert r.status == "success"
    assert [s.segment_id for s in r.steps] == ["toW", "wtoZ"]
    # t=5 到 W，[5,15) 与 [0,20) 相交，等到 20，30 到达
    assert r.total_wait == 15 and r.arrival == 30
    assert r.waits[0].node_id == "W"


def test_unreachable_disconnected_graph():
    r = plan_route(
        [seg("r1", "A", "B", 10)], nodes({"A": False, "B": False, "C": False}),
        "A", "C", set(),
    )
    assert r.status == "infeasible"
    assert r.blockages[0].type == "unreachable"
    assert (r.blockages[0].origin, r.blockages[0].destination) == ("A", "C")


def test_blockages_stable_sorted():
    # 节点 A 有两条出边：一条禁运、一条窗口关闭且禁等；外加一条到孤岛 B 的边
    segments = [
        seg("z1", "A", "X", 5, embargo={"3"}),
        seg("a2", "A", "Y", 5, windows=[(0, 10)]),
        seg("a3", "A", "B", 5),
    ]
    r = plan_route(
        segments,
        nodes({"A": False, "X": False, "Y": False, "B": False, "Z": False}),
        "A", "Z", {"3"},
    )
    assert r.status == "infeasible"
    types = [(b.type, b.node_id, b.segment_id) for b in r.blockages]
    # embargo 类型优先，随后按节点、路段 ID 字典序；a2(no_waiting) 在 a3 之后无路扩展
    # a3 指向 B（出度 0），其下游不产生边级阻断；只应有 z1 与 a2 两条
    assert types == [("embargo", "A", "z1"), ("no_waiting", "A", "a2")]


def test_result_is_deterministic():
    segments = [
        seg("a1", "A", "M", 5), seg("a2", "M", "Z", 5),
        seg("b1", "A", "N", 5), seg("b2", "N", "Z", 5),
        seg("r1", "A", "Z", 20, windows=[(0, 10)]),
    ]
    wn = nodes({"A": True, "M": False, "N": False, "Z": False})
    paths = [
        tuple(s.segment_id for s in plan_route(segments, wn, "A", "Z", set()).steps)
        for _ in range(5)
    ]
    assert all(p == paths[0] for p in paths)


def test_lexicographic_requires_equal_drive_labels_both_kept():
    # 回归：允许等待节点上 drive 相同、t 不同的两个标号不能互相支配
    # （等待会抹平到达差，需靠路段序列字典序裁决）。
    # S5/S6 都从 N2 到 N3（7 分钟）：S5 在起点等待到窗口结束，11 到 N3（已等4）；
    # S6 不等待 7 到 N3，随后 S3 在 N3 等待 4。两者到达/等待相同，S5 字典序更小。
    segments = [
        seg("S2", "N0", "N1", 3, embargo={"8"}),
        seg("S3", "N3", "N0", 8, embargo={"6.1"}, windows=[(2, 8), (8, 11)]),
        seg("S5", "N2", "N3", 7, embargo={"6.1", "8"}, windows=[(2, 4)]),
        seg("S6", "N2", "N3", 7, embargo=set(), windows=[(12, 18)]),
    ]
    r = plan_route(
        segments,
        nodes({"N0": True, "N1": True, "N2": True, "N3": True}),
        "N2", "N1", {"5.1", "3"},
    )
    assert r.status == "success"
    assert (r.arrival, r.total_wait) == (22, 4)
    assert [s.segment_id for s in r.steps] == ["S5", "S3", "S2"]


def test_no_waiting_node_later_label_can_enter_window_gap():
    # 回归：禁等节点上 t=6 的标号被窗口拦截，而 t=13 的标号恰在窗口结束后通过；
    # 早到标号不得支配晚到标号。
    segments = [
        seg("S4", "N0", "N1", 4, embargo={"5.1", "8"}, windows=[(3, 9)]),
        seg("S6", "N1", "N2", 4, windows=[(4, 12)]),
        seg("S0", "N0", "N1", 6, embargo={"8", "6.1"}),
    ]
    r = plan_route(
        segments,
        nodes({"N0": True, "N1": False, "N2": True}),
        "N0", "N2", {"3"},
    )
    assert r.status == "success"
    # S4 在 N0 等到 9 进入，13 到 N1；S6 [13,17) 与 [4,12) 不相交，17 到达
    assert [s.segment_id for s in r.steps] == ["S4", "S6"]
    assert (r.arrival, r.total_wait) == (17, 9)
    # S0 被类别 3 放行？不——S0 禁运 {8,6.1}，与 {3} 不相交，且 t=0 走 S0 6 到 N1，
    # S6 [6,10) 与 [4,12) 相交且 N1 禁等 => S0 走法失败但 S4 走法成功
    # 阻断列表中不得出现 S6（存在标号成功通过），S0 因该标号窗口失败但边被通过也不报
    assert r.blockages == []


def test_equal_arrival_less_wait_not_pruned_by_first_dest_label():
    # 回归：S4 与 S7 都从 N1 直达 N0，均 16 到达，但 S7 仅等待 8 < S4 的 9。
    # 首个到达终点的标号不得按到达时刻剪去同刻更优（等待更少）的标号。
    segments = [
        seg("S4", "N1", "N0", 7, windows=[(0, 4), (5, 9)]),
        seg("S7", "N1", "N0", 8, windows=[(3, 8)]),
    ]
    r = plan_route(segments, nodes({"N0": True, "N1": True}), "N1", "N0",
                   {"5.1", "8", "3"})
    assert r.status == "success"
    assert (r.arrival, r.total_wait) == (16, 8)
    assert [s.segment_id for s in r.steps] == ["S7"]


def test_step_timestamps_are_consistent():
    segments = [
        seg("r2", "A", "M", 6, windows=[(3, 8)]),
        seg("r3", "M", "Z", 4),
    ]
    r = plan_route(segments, nodes({"A": True, "M": False, "Z": False}), "A", "Z", set())
    # t=0 时 [0,6) 与 [3,8) 相交 => 等至 8，14 到 M；r3 [14,18)
    assert r.status == "success"
    s1, s2 = r.steps
    assert (s1.departure, s1.arrival, s1.wait_before_minutes) == (8, 14, 8)
    assert (s2.departure, s2.arrival, s2.wait_before_minutes) == (14, 18, 0)
    assert s1.arrival == s2.departure
    assert isinstance(r, RoutePlan)


def test_no_waiting_graph_allows_repeating_segments_via_loop():
    # 回归：全部节点禁等，车辆无法原地等待，唯一可行路线必须连续环行消磨时间。
    # C1=S->X 4 分钟，C2=X->S 4 分钟，SB=S->B 1 分钟，BD=B->D 1 分钟；
    # BD 在出发起的 [0,17) 禁行且无类别禁运。
    # 直达：1 到 B 即进 BD，受阻；绕一圈：8 回 S、9 到 B 进 BD，仍受阻；
    # 绕两圈：16 回 S、17 到 B，恰在窗口结束的第 17 分钟进入 BD，18 到达。
    segments = [
        seg("C1", "S", "X", 4),
        seg("C2", "X", "S", 4),
        seg("SB", "S", "B", 1),
        seg("BD", "B", "D", 1, windows=[(0, 17)]),
    ]
    wn = nodes({"S": False, "X": False, "B": False, "D": False})
    r = plan_route(segments, wn, "S", "D", set())
    assert r.status == "success"
    assert [s.segment_id for s in r.steps] == ["C1", "C2", "C1", "C2", "SB", "BD"]
    assert (r.arrival, r.total_drive, r.total_wait) == (18, 18, 0)
    assert [s.departure for s in r.steps] == [0, 4, 8, 12, 16, 17]
    assert [s.arrival for s in r.steps] == [4, 8, 12, 16, 17, 18]
    assert all(s.wait_before_minutes == 0 for s in r.steps)
    assert r.waits == [] and r.blockages == []
    # 逐段衔接：前段到达即后段出发（零等待）
    assert all(a == b for a, b in zip(
        [s.arrival for s in r.steps], [s.departure for s in r.steps[1:]]))
    # 结果可复现
    r2 = plan_route(segments, wn, "S", "D", set())
    assert [s.segment_id for s in r2.steps] == [s.segment_id for s in r.steps]


def test_looping_cannot_clear_embargo_and_search_terminates():
    # 回归：恒闭禁运不能靠环行解除。图中含可无限重复的环（C1/C2），仍须在
    # 有限时间内判定不可行：不返回部分行程，阻断原因稳定为 B->BD 的 embargo。
    segments = [
        seg("C1", "S", "X", 4),
        seg("C2", "X", "S", 4),
        seg("SB", "S", "B", 1),
        seg("BD", "B", "D", 1, embargo={"3"}, windows=[(0, 17)]),
    ]
    r = plan_route(
        segments, nodes({"S": False, "X": False, "B": False, "D": False}),
        "S", "D", {"3"},
    )
    assert r.status == "infeasible"
    assert r.steps == [] and r.waits == []
    assert [(b.type, b.node_id, b.segment_id) for b in r.blockages] == [
        ("embargo", "B", "BD")]
    assert r.blockages[0].matched_classes == ("3",)


def test_loop_burns_time_until_far_window_end():
    # 环长 8 分钟，窗口 [0,100)：需要 8k+1 >= 100 => k=13，105 进 BD、106 到达，
    # 全程零等待；验证有限终止界内多圈搜索与最早到达裁决。
    segments = [
        seg("C1", "S", "X", 4),
        seg("C2", "X", "S", 4),
        seg("SB", "S", "B", 1),
        seg("BD", "B", "D", 1, windows=[(0, 100)]),
    ]
    r = plan_route(
        segments, nodes({"S": False, "X": False, "B": False, "D": False}),
        "S", "D", set(),
    )
    assert r.status == "success"
    assert r.arrival == 106 and r.total_wait == 0
    assert [s.segment_id for s in r.steps].count("C1") == 13
    assert r.steps[-1].departure == 105


def test_looping_path_beats_waiting_on_wait_tiebreak():
    # 允许等待时环行仍可参与裁决：直达 G 在 S 等待 17（18 到、等 17），
    # 两圈 C1/C2 后 t=16 再等 1，同样 18 到但仅等 1 => 总等待最少者胜。
    segments = [
        seg("C1", "S", "X", 4),
        seg("C2", "X", "S", 4),
        seg("G", "S", "D", 1, windows=[(0, 17)]),
    ]
    r = plan_route(
        segments, nodes({"S": True, "X": False, "D": False}), "S", "D", set(),
    )
    assert r.status == "success"
    assert (r.arrival, r.total_wait) == (18, 1)
    assert [s.segment_id for s in r.steps] == ["C1", "C2", "C1", "C2", "G"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} 个测试全部通过")

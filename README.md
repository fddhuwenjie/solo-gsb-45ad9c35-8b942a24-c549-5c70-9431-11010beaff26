# 危险化学品同车分舱规划 API

基于 **Python + FastAPI + SQLite + OR-Tools (CP-SAT)** 的危险化学品同车分舱规划服务。

- 输入货物（编号、危险类别、数量）、禁配关系、舱室容量与指定舱室条件；
- 输出满足**容量限制**与**隔离规则**的完整分舱方案；
- 优化目标：**先最少舱室**，再按**舱室编号 → 货物编号**稳定分配，相同输入结果完全一致；
- 仅当 CP-SAT **证明不可行**时才报告冲突，**不返回部分方案**，而是列出导致冲突的货物与规则；
- 求解超时（UNKNOWN）不误判为不可行：第一阶段超时返回 `undetermined`，
  第二阶段超时回退第一阶段完整解并标记 `stable_order_proven=false`；
- 方案落库持久化，支持按方案编号查询；
- 启动时自动初始化示例禁配规则；
- **现场执行模块**：为 success 方案创建执行单并冻结货物与舱室分配；批量接收扫描事件，
  按序号连续、同事务按序应用，违规整批回滚并返回首个错误与提交前状态；
  event_id 幂等去重，全部卸完自动完成。
- **危险品运输路线编排**：为 success 方案提交本地有向道路图（路段 ID、行驶分钟、
  禁运类别、UTC 禁行窗口、节点是否允许等待）与起终点、出发时间；危险类别自动从
  方案**原始货物**提取。禁行窗口按 `[start,end)` 半开处理，通行区间与其相交即不可
  进入；可在允许等待的节点停留到下一可行时刻。**所有节点均禁止等待时，允许通过
  连续环行（重复经过同一路段）推移到达时刻**，搜索在窗口最晚结束时刻后转为静态
  最短路，保证有限时间结束。按 **最早到达 → 总等待最少 →
  路段 ID 序列字典序**确定唯一结果，返回逐段时刻与等待记录；无可行路线时返回
  **稳定排序的阻断原因**，不给出部分行程。结果持久化，支持按编号查询。

## 快速启动

```bash
docker compose up --build
```

- 服务端口：**8000**
- 交互式文档：http://localhost:8000/docs
- SQLite 数据持久化在 named volume `hazmat-data`（容器内 `/data/hazmat.db`）

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
DATABASE_URL="sqlite:///./hazmat.db" uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行单元测试：

```bash
python tests/test_solver.py
python tests/test_execution.py
python tests/test_routing.py
python tests/test_routes.py
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/rules` | 查看内置类别级禁配规则（启动自动初始化） |
| POST | `/plans` | 创建分舱规划方案 |
| GET | `/plans` | 方案摘要列表 |
| GET | `/plans/{plan_id}` | 按方案编号查询结果 |
| POST | `/plans/{plan_id}/executions` | 为 success 方案创建现场执行单（冻结分配） |
| POST | `/executions/{execution_id}/events` | 批量提交扫描事件（按序应用、幂等、整批回滚） |
| GET | `/executions/{execution_id}` | 查询执行单：最后序号、每件货物状态、事件日志 |
| POST | `/plans/{plan_id}/routes` | 为 success 方案编排危险品运输路线（201） |
| GET | `/plans/{plan_id}/routes` | 列出该方案下的路线摘要 |
| GET | `/routes/{route_id}` | 按路线编号查询编排结果（逐段时刻、等待/阻断） |

## 请求示例

### 1. 创建方案（可行）

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "cargos": [
      {"cargo_id": "G001", "hazard_class": "3",   "quantity": 10},
      {"cargo_id": "G002", "hazard_class": "5.1", "quantity": 8},
      {"cargo_id": "G003", "hazard_class": "3",   "quantity": 6},
      {"cargo_id": "G004", "hazard_class": "8",   "quantity": 12}
    ],
    "compartments": [
      {"compartment_id": "1", "capacity": 25},
      {"compartment_id": "2", "capacity": 25},
      {"compartment_id": "3", "capacity": 25}
    ],
    "assignments": [{"cargo_id": "G004", "compartment_id": "3"}]
  }'
```

响应（`status=success`）：类别 3 与 5.1 自动隔离，G004 满足指定舱室，仅使用 2 个舱室：

```json
{
  "plan_id": 1,
  "status": "success",
  "proven_optimal": true,
  "stable_order_proven": true,
  "compartments_used": 2,
  "assignments": [
    {"cargo_id": "G001", "compartment_id": "1"},
    {"cargo_id": "G002", "compartment_id": "3"},
    {"cargo_id": "G003", "compartment_id": "1"},
    {"cargo_id": "G004", "compartment_id": "3"}
  ],
  "compartment_loads": [
    {"compartment_id": "1", "load": 16, "capacity": 25, "cargo_ids": ["G001", "G003"]},
    {"compartment_id": "3", "load": 20, "capacity": 25, "cargo_ids": ["G002", "G004"]}
  ],
  "conflicts": [],
  "created_at": "2026-09-22T04:19:08.747441"
}
```

### 2. 无可行解（返回冲突而非部分方案）

3 种货物两两禁配但只有 2 个舱室：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "cargos": [
      {"cargo_id": "A1", "hazard_class": "3",   "quantity": 10},
      {"cargo_id": "A2", "hazard_class": "5.1", "quantity": 10},
      {"cargo_id": "A3", "hazard_class": "6.1", "quantity": 10}
    ],
    "compartments": [
      {"compartment_id": "1", "capacity": 30},
      {"compartment_id": "2", "capacity": 30}
    ],
    "incompatibilities": [
      {"cargo_a": "A2", "cargo_b": "A3", "rule": "托运人指定：A2 与 A3 不得同舱"}
    ]
  }'
```

响应（`status=infeasible`，`assignments` 为空，`conflicts` 列出冲突货物与规则）：

```json
{
  "plan_id": 2,
  "status": "infeasible",
  "proven_optimal": false,
  "stable_order_proven": false,
  "compartments_used": 0,
  "assignments": [],
  "conflicts": [
    {
      "type": "incompatibility",
      "cargo_ids": ["A1", "A2", "A3"],
      "rule": "托运人指定：A2 与 A3 不得同舱; 类别 3 与 5.1 禁配（…）; 类别 3 与 6.1 禁配（…）",
      "detail": "3 种货物两两禁配，至少需要 3 个舱室，但仅有 2 个"
    }
  ]
}
```

冲突类型（`type`）：`capacity`（容量不足）、`incompatibility`（禁配隔离冲突）、
`assignment`（指定舱室条件矛盾）、`mixed`（容量与禁配组合导致）。

### 3. 求解超时（undetermined，而非误判不可行）

第一阶段在 `time_limit_seconds` 内既未找到解也未证明不可行（CP-SAT 返回 UNKNOWN）时，
响应为 `status=undetermined`，`assignments` 与 `conflicts` 均为空，方案以此状态落库，
**不会被保存为 infeasible**——只有 CP-SAT 证明 INFEASIBLE 才会报告冲突：

```json
{
  "plan_id": 3,
  "status": "undetermined",
  "proven_optimal": false,
  "stable_order_proven": false,
  "compartments_used": 0,
  "assignments": [],
  "compartment_loads": [],
  "conflicts": []
}
```

若第一阶段已找到完整解、仅第二阶段（稳定次序优化）超时，则**回退第一阶段完整解**，
返回 `status=success`、`proven_optimal=false`、`stable_order_proven=false`
（稳定次序未获证明）；第二阶段证得最优时 `stable_order_proven=true`。

### 4. 按方案编号查询

```bash
curl http://localhost:8000/plans/1
```

## 现场执行

以示例 1 的方案（plan_id=1）为例：G001/G003 计划舱室 `1`，G002 计划舱室 `2`。

### 5. 创建执行单（冻结方案分配）

仅 `status=success` 的方案可创建，否则返回 **409**。创建时冻结方案中的货物与舱室分配，
各货物状态为 `pending`（未装），`last_sequence` 从 0 开始：

```bash
curl -X POST http://localhost:8000/plans/1/executions
```

响应：

```json
{
  "execution_id": 1,
  "plan_id": 1,
  "status": "in_progress",
  "last_sequence": 0,
  "cargos": [
    {"cargo_id": "G001", "planned_compartment_id": "1", "status": "pending"},
    {"cargo_id": "G002", "planned_compartment_id": "2", "status": "pending"},
    {"cargo_id": "G003", "planned_compartment_id": "1", "status": "pending"}
  ],
  "events": [],
  "created_at": "2026-09-27T07:11:38.540175",
  "updated_at": "2026-09-27T07:11:38.540181"
}
```

### 6. 批量提交扫描事件

`POST /executions/{execution_id}/events`，一批事件在**同一事务**中按序应用：

- `sequence` 从 **1** 开始连续递增，新事件必须接续当前 `last_sequence`；
- 货物必须装入**计划舱室**，不得重复装载、不得未装先卸；
- **开始卸载前须完成全部装载**（卸载开始后不得再装载）；
- 任一新事件违规 → **整批回滚（422）**，返回首个错误及**提交前状态**；
- 相同 `event_id` + 相同载荷的重试 → 幂等跳过、不重复推进；
  相同 `event_id` + 不同载荷 → **409**。

先装载全部货物：

```bash
curl -X POST http://localhost:8000/executions/1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"event_id": "sc-1", "sequence": 1, "cargo_id": "G001", "action": "load",   "compartment_id": "1"},
      {"event_id": "sc-2", "sequence": 2, "cargo_id": "G002", "action": "load",   "compartment_id": "2"},
      {"event_id": "sc-3", "sequence": 3, "cargo_id": "G003", "action": "load",   "compartment_id": "1"}
    ]
  }'
```

响应（`applied`=新应用条数，`skipped`=幂等重试跳过条数）：

```json
{
  "execution_id": 1,
  "applied": 3,
  "skipped": 0,
  "status": "in_progress",
  "last_sequence": 3,
  "cargos": [
    {"cargo_id": "G001", "planned_compartment_id": "1", "status": "loaded"},
    {"cargo_id": "G002", "planned_compartment_id": "2", "status": "loaded"},
    {"cargo_id": "G003", "planned_compartment_id": "1", "status": "loaded"}
  ]
}
```

相同载荷重试（网络重发）→ `applied=0, skipped=2`，`last_sequence` 保持 3：

```bash
curl -X POST http://localhost:8000/executions/1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"event_id": "sc-1", "sequence": 1, "cargo_id": "G001", "action": "load", "compartment_id": "1"},
      {"event_id": "sc-2", "sequence": 2, "cargo_id": "G002", "action": "load", "compartment_id": "2"}
    ]
  }'
```

全部卸完 → 执行单**自动 completed**：

```bash
curl -X POST http://localhost:8000/executions/1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"event_id": "u-1", "sequence": 4, "cargo_id": "G001", "action": "unload", "compartment_id": "1"},
      {"event_id": "u-2", "sequence": 5, "cargo_id": "G002", "action": "unload", "compartment_id": "2"},
      {"event_id": "u-3", "sequence": 6, "cargo_id": "G003", "action": "unload", "compartment_id": "1"}
    ]
  }'
```

```json
{
  "execution_id": 1,
  "applied": 3,
  "skipped": 0,
  "status": "completed",
  "last_sequence": 6,
  "cargos": [
    {"cargo_id": "G001", "planned_compartment_id": "1", "status": "unloaded"},
    {"cargo_id": "G002", "planned_compartment_id": "2", "status": "unloaded"},
    {"cargo_id": "G003", "planned_compartment_id": "1", "status": "unloaded"}
  ]
}
```

违规示例：G002 计划舱室是 `2` 却扫到 `1`，整批回滚（同批合法的 sc-1 也不落库），
响应中 `detail.state` 为提交前状态：

```bash
curl -X POST http://localhost:8000/executions/1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"event_id": "sc-1", "sequence": 1, "cargo_id": "G001", "action": "load", "compartment_id": "1"},
      {"event_id": "sc-2", "sequence": 2, "cargo_id": "G002", "action": "load", "compartment_id": "1"}
    ]
  }'
# HTTP 422
```

```json
{
  "detail": {
    "error": "货物 G002 必须装入计划舱室 2，扫描到的是 1",
    "event_id": "sc-2",
    "sequence": 2,
    "state": {
      "execution_id": 1,
      "status": "in_progress",
      "last_sequence": 0,
      "cargos": [
        {"cargo_id": "G001", "planned_compartment_id": "1", "status": "pending"},
        {"cargo_id": "G002", "planned_compartment_id": "2", "status": "pending"},
        {"cargo_id": "G003", "planned_compartment_id": "1", "status": "pending"}
      ]
    }
  }
}
```

事件字段与状态语义：

| 字段 | 说明 |
| --- | --- |
| `event_id` | 事件唯一编号，幂等键 |
| `sequence` | 连续序号，从 1 开始，必须等于当前 `last_sequence + 1` |
| `cargo_id` | 扫描货物编号，必须属于该执行单 |
| `action` | `load`（装舱）/ `unload`（卸舱） |
| `compartment_id` | 扫描舱室，必须等于该货物的计划舱室 |

货物状态机：`pending`（未装）→ `loaded`（在舱）→ `unloaded`（已卸）。

错误码：`409`（为非 success 方案建单 / 相同 event_id 载荷不一致）、
`422`（业务规则或序号违规，返回首个错误与提交前状态）、`404`（方案或执行单不存在）。

### 7. 查询执行单

```bash
curl http://localhost:8000/executions/1
```

返回执行单状态、`last_sequence`、每件货物的计划舱室与当前状态，以及按序号升序的事件日志。

## 危险品运输路线编排

为 `status=success` 的方案编排运输路线（否则 **409**）。危险类别**无需在请求中给出**，
服务自动从方案的**原始货物**（`POST /plans` 的 `cargos`）提取去重集合；路段的
`embargo_classes` 与该集合相交即为**恒闭**，等待不能解除。

### 8. 编排路线（含等待、禁运与逐段时刻）

道路图为有向图：每个路段给出 `segment_id`、`from_node`、`to_node`、
`drive_minutes`（正整数分钟）、`embargo_classes`（禁运类别）、`windows`
（UTC 禁行窗口）；每个节点给出 `allow_waiting`。所有时刻必须**带时区**并对齐到
**整分钟**（内部统一按 UTC 计算）。

```bash
curl -X POST http://localhost:8000/plans/1/routes \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"node_id": "depot",    "allow_waiting": true},
      {"node_id": "junction", "allow_waiting": false},
      {"node_id": "site",     "allow_waiting": false}
    ],
    "segments": [
      {"segment_id": "R1", "from_node": "depot", "to_node": "site",
       "drive_minutes": 40, "embargo_classes": ["1"],
       "windows": [{"start": "2026-09-29T08:00:00Z", "end": "2026-09-29T08:30:00Z"}]},
      {"segment_id": "R2", "from_node": "depot", "to_node": "junction",
       "drive_minutes": 20, "embargo_classes": [], "windows": []},
      {"segment_id": "R3", "from_node": "junction", "to_node": "site",
       "drive_minutes": 20, "embargo_classes": [], "windows": []}
    ],
    "origin": "depot",
    "destination": "site",
    "departure_at": "2026-09-29T08:00:00Z"
  }'
```

方案 1 的原始货物含类别 `3`、`5.1`：R1 的禁运类别 `1` 不相交，但其窗口
`[08:00,08:30)` 与从 08:00 出发的通行区间 `[08:00,08:40)` 相交；在 depot 等待到
08:30 出发会 09:10 到达，而绕行 R2+R3 全程 40 分钟、08:40 到达——**最早到达**
优先，故选择 R2、R3：

```json
{
  "route_id": 1,
  "plan_id": 1,
  "status": "success",
  "origin": "depot",
  "destination": "site",
  "departure_at": "2026-09-29T08:00:00Z",
  "arrival_at": "2026-09-29T08:40:00Z",
  "total_drive_minutes": 40,
  "total_wait_minutes": 0,
  "segment_ids": ["R2", "R3"],
  "hazard_classes": ["3", "5.1"],
  "steps": [
    {"segment_id": "R2", "from_node": "depot", "to_node": "junction",
     "departure_at": "2026-09-29T08:00:00Z", "arrival_at": "2026-09-29T08:20:00Z",
     "drive_minutes": 20, "wait_before_minutes": 0},
    {"segment_id": "R3", "from_node": "junction", "to_node": "site",
     "departure_at": "2026-09-29T08:20:00Z", "arrival_at": "2026-09-29T08:40:00Z",
     "drive_minutes": 20, "wait_before_minutes": 0}
  ],
  "waits": [],
  "blockages": []
}
```

**节点等待示例**：若仅保留 R1，depot 允许等待，则停留到窗口结束的 08:30
（`[08:30,09:10)` 与 `[08:00,08:30)` 半开不相交）再进入。响应中：

- `arrival_at` 为 `09:10:00Z`，`total_wait_minutes` 为 30；
- `steps[0].departure_at` 为 `08:30:00Z`、`wait_before_minutes` 为 30；
- `waits` 给出等待记录：`node_id=depot`、`for_segment_id=R1`、
  `start_at=08:00`、`end_at=08:30`、`wait_minutes=30`，以及触发等待的
  `blocking_windows`（绝对 UTC 时刻）。

时间可用带偏移量的写法（如 `2026-09-29T16:00:00+08:00`），服务统一换算为 UTC。

判优规则（结果唯一、可复现）：

1. **最早到达** `arrival_at`；
2. 到达时刻相同 → **总等待最少** `total_wait_minutes`（多行驶、少等待者胜，
   因为等待会把出发推迟到同一窗口边界，可能抹平到达差）；
3. 仍相同 → **路段 ID 序列字典序**（逐段比较 `segment_ids`）。

### 9. 禁等节点图中必须环行（允许重复经过同一路段）

当路径上**所有节点都禁止等待**时，车辆不能原地停留，只能通过连续环行把到达
时刻推移到窗口结束之后——合法路线会重复经过同一路段，搜索允许这种环行并在
有限时间内结束（窗口最晚结束时刻之后，问题退化为静态最短路）。

例：`C1` 为 S→X（4 分钟）、`C2` 为 X→S（4 分钟）、`SB` 为 S→B（1 分钟）、
`BD` 为 B→D（1 分钟）；`BD` 从出发起的 `[08:00,08:17)` 禁行且**无类别禁运**。
直达（第 1 分钟进 BD）或只绕一圈（第 9 分钟进 BD）都受阻；车辆须连续两次走
C1、C2（第 16 分钟回到 S），再走 SB、BD，**恰在第 17 分钟进入 BD**：

```bash
curl -X POST http://localhost:8000/plans/1/routes \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"node_id": "S", "allow_waiting": false},
      {"node_id": "X", "allow_waiting": false},
      {"node_id": "B", "allow_waiting": false},
      {"node_id": "D", "allow_waiting": false}
    ],
    "segments": [
      {"segment_id": "C1", "from_node": "S", "to_node": "X",
       "drive_minutes": 4, "embargo_classes": [], "windows": []},
      {"segment_id": "C2", "from_node": "X", "to_node": "S",
       "drive_minutes": 4, "embargo_classes": [], "windows": []},
      {"segment_id": "SB", "from_node": "S", "to_node": "B",
       "drive_minutes": 1, "embargo_classes": [], "windows": []},
      {"segment_id": "BD", "from_node": "B", "to_node": "D",
       "drive_minutes": 1, "embargo_classes": [],
       "windows": [{"start": "2026-09-29T08:00:00Z", "end": "2026-09-29T08:17:00Z"}]}
    ],
    "origin": "S",
    "destination": "D",
    "departure_at": "2026-09-29T08:00:00Z"
  }'
```

响应返回**完整逐段时刻**与**零等待记录**（环行是行驶而非等待，
`total_wait_minutes=0`），结果同样持久化、可按编号查询：

```json
{
  "route_id": 3,
  "plan_id": 1,
  "status": "success",
  "origin": "S",
  "destination": "D",
  "departure_at": "2026-09-29T08:00:00Z",
  "arrival_at": "2026-09-29T08:18:00Z",
  "total_drive_minutes": 18,
  "total_wait_minutes": 0,
  "segment_ids": ["C1", "C2", "C1", "C2", "SB", "BD"],
  "hazard_classes": ["3", "5.1"],
  "steps": [
    {"segment_id": "C1", "from_node": "S", "to_node": "X",
     "departure_at": "2026-09-29T08:00:00Z", "arrival_at": "2026-09-29T08:04:00Z",
     "drive_minutes": 4, "wait_before_minutes": 0},
    {"segment_id": "C2", "from_node": "X", "to_node": "S",
     "departure_at": "2026-09-29T08:04:00Z", "arrival_at": "2026-09-29T08:08:00Z",
     "drive_minutes": 4, "wait_before_minutes": 0},
    {"segment_id": "C1", "from_node": "S", "to_node": "X",
     "departure_at": "2026-09-29T08:08:00Z", "arrival_at": "2026-09-29T08:12:00Z",
     "drive_minutes": 4, "wait_before_minutes": 0},
    {"segment_id": "C2", "from_node": "X", "to_node": "S",
     "departure_at": "2026-09-29T08:12:00Z", "arrival_at": "2026-09-29T08:16:00Z",
     "drive_minutes": 4, "wait_before_minutes": 0},
    {"segment_id": "SB", "from_node": "S", "to_node": "B",
     "departure_at": "2026-09-29T08:16:00Z", "arrival_at": "2026-09-29T08:17:00Z",
     "drive_minutes": 1, "wait_before_minutes": 0},
    {"segment_id": "BD", "from_node": "B", "to_node": "D",
     "departure_at": "2026-09-29T08:17:00Z", "arrival_at": "2026-09-29T08:18:00Z",
     "drive_minutes": 1, "wait_before_minutes": 0}
  ],
  "waits": [],
  "blockages": []
}
```

若上例中 `BD` 改为对本车载货类别恒闭（`embargo_classes` 相交），环行再多圈
也无法解除，仍返回 `status=infeasible` 且阻断原因稳定为 `B`→`BD` 的
`embargo`，绝不给出部分行程。

### 10. 无可行路线（阻断原因，而非部分行程）

`status=infeasible` 时 `steps`、`waits`、`segment_ids` 必为空，
`arrival_at=null`，`blockages` 给出**稳定排序**的阻断原因
（先 `type`：`embargo` → `no_waiting`，再节点 ID、路段 ID）。例如下方路段对
类别 `3` 恒闭：

```bash
curl -X POST http://localhost:8000/plans/1/routes \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"node_id": "depot", "allow_waiting": false},
      {"node_id": "site",  "allow_waiting": false}
    ],
    "segments": [
      {"segment_id": "X1", "from_node": "depot", "to_node": "site",
       "drive_minutes": 10, "embargo_classes": ["3"], "windows": []}
    ],
    "origin": "depot",
    "destination": "site",
    "departure_at": "2026-09-29T08:00:00Z"
  }'
```

```json
{
  "route_id": 2,
  "plan_id": 1,
  "status": "infeasible",
  "arrival_at": null,
  "total_drive_minutes": 0,
  "total_wait_minutes": 0,
  "segment_ids": [],
  "hazard_classes": ["3", "5.1"],
  "steps": [],
  "waits": [],
  "blockages": [
    {"type": "embargo", "node_id": "depot", "segment_id": "X1", "to_node": "site",
     "embargo_classes": ["3"], "matched_classes": ["3"],
     "arrival_at": null, "drive_minutes": null, "windows": []}
  ]
}
```

阻断原因类型：

| type | 含义 | 关键字段 |
| --- | --- | --- |
| `embargo` | 路段禁运类别与本车载货类别相交（恒闭，等待无效） | `embargo_classes`（路段全部禁运类别）、`matched_classes`（与载货相交部分） |
| `no_waiting` | 最早到达时通行区间与禁行窗口相交，而该节点 `allow_waiting=false` | `arrival_at`（最早到达时刻）、`drive_minutes`、`windows`（相交窗口） |
| `unreachable` | 起终点不连通等兜底情形（无任何边级阻断可报告时） | `origin`、`destination` |

窗口半开语义 `[start,end)`：通行区间 `[t, t+drive)` 与窗口相交的判定为
`t < end 且 t+drive > start`；因此**恰在窗口结束时刻进入**（`t == end`）或
**恰在窗口开始时刻到达**（`t+drive == start`）均允许。

### 11. 按编号查询路线

```bash
curl http://localhost:8000/routes/1
curl http://localhost:8000/plans/1/routes   # 列出该方案下全部路线摘要
```

路线结果（含 infeasible 的阻断原因、道路图与请求快照）持久化在 SQLite 中，
重启容器后仍可按编号查询。错误码：`404`（方案或路线不存在）、
`409`（为非 success 方案编排）、`422`（图引用错误、时刻不带时区或未对齐整分钟）。

## 请求字段说明

| 字段 | 说明 |
| --- | --- |
| `cargos` | 货物清单：`cargo_id`（编号）、`hazard_class`（危险类别）、`quantity`（数量，正整数） |
| `compartments` | 舱室清单：`compartment_id`（编号）、`capacity`（容量，正整数） |
| `incompatibilities` | 可选，货物级禁配关系，补充内置类别规则 |
| `assignments` | 可选，指定舱室条件：货物**必须**放入该舱室 |
| `forbidden_assignments` | 可选，指定舱室条件：货物**禁止**放入该舱室 |
| `use_default_rules` | 可选，默认 `true`，是否启用内置类别级禁配规则 |
| `time_limit_seconds` | 可选，默认 30，单次求解时间上限 |

路线请求（`POST /plans/{plan_id}/routes`）字段：

| 字段 | 说明 |
| --- | --- |
| `nodes` | 道路节点：`node_id`、`allow_waiting`（是否允许等待，默认 `false`） |
| `segments` | 有向路段：`segment_id`、`from_node`、`to_node`、`drive_minutes`（≥1 分钟）、`embargo_classes`（禁运类别，恒闭）、`windows`（UTC 禁行窗口 `[start,end)`） |
| `origin` / `destination` | 起点 / 终点节点编号（必须在 `nodes` 中） |
| `departure_at` | 出发时间，**必须带时区**并对齐整分钟，内部按 UTC 计算 |
| （危险类别） | **不在请求中提供**，自动从方案原始货物的 `hazard_class` 去重提取 |

## 结果稳定性说明

求解器分两个阶段：先最小化使用舱室数；舱室数固定后，最小化"货物序号加权的舱室编号"，
使小编号货物优先进入小编号舱室。配合单线程搜索与固定随机种子，**相同输入必定得到相同方案**。

响应字段与求解状态的对应关系：

| 字段 | 含义 |
| --- | --- |
| `status` | `success`（求得完整方案）/ `infeasible`（CP-SAT 证明不可行）/ `undetermined`（第一阶段超时未得出结论） |
| `proven_optimal` | 两个阶段均在时限内证得最优时为 `true`；否则为 `false`（返回当前最优可行解） |
| `stable_order_proven` | 第二阶段稳定次序证得最优时为 `true`；第二阶段超时回退第一阶段解时为 `false` |

`status` 与上述字段在创建（`POST /plans`）、列表（`GET /plans`）与按编号查询
（`GET /plans/{plan_id}`）中一致持久化。

## 项目结构

```
app/
  main.py          # FastAPI 入口与接口
  solver.py        # CP-SAT 两阶段求解 + 不可行冲突诊断
  executions.py    # 现场执行：执行单创建、扫描事件批次应用与状态查询
  routing.py       # 时间相关多标号最短路：禁运、半开窗口、节点等待、环行、阻断收集
  route_service.py # 路线编排服务：危险类别提取、UTC 换算、持久化与查询
  schemas.py       # 请求/响应模型与校验
  models.py        # ORM：方案、禁配规则、执行单、货物状态、事件记录、路线
  database.py      # SQLite 连接
  seed.py          # 示例禁配规则初始化
tests/
  test_solver.py
  test_execution.py
  test_routing.py
  test_routes.py
Dockerfile
docker-compose.yml
requirements.txt
```

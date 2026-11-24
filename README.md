# 巡展道具箱 · 点阵牌识别 API

FastAPI + SQLite 实现的 5×5 黑白点阵牌册与确认读数服务。临时添牌也必须保住
已确认箱号。另含**单台扫码夹具的开孔方案 API**：提交最多 12 个挡格与可开位，
给出最少开孔方案，采用后若被合法新牌破坏唯一性则随挂牌原子撤下——见下文
[开孔方案 API](#开孔方案-api单台扫码夹具)。

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs
```

Compose 启动时会自动幂等写入演示数据（`SEED_DEMO=1`）。在 `/docs` 的
`POST /api/boxes` 里直接 **Try it out → Execute**（请求体已预填“拦截牌”），
即可看到一条四角遮挡的已确认读数拦下新牌：

```json
HTTP 409
{
  "error": "registration_conflict",
  "conflicting_box_numbers": ["BOX-DEMO-01"],
  "conflicting_box_number": "BOX-DEMO-01",
  "related_reading_ids": ["RD-0001"],
  "related_reading_id": "RD-0001",
  "conflicts": [{
    "kind": "confirmed_reading_matches_new_tag",
    "box_number": "BOX-DEMO-01",
    "reading_id": "RD-0001",
    "match": {"orientation": 0, "mismatches": 0, "visible_cells": 21}
  }],
  "index_audit": {"full_reading_table_scan": false,
                  "confirmed_readings_total": 1,
                  "chunk_index_candidates": 1,
                  "verified_conflicts": 1}
}
```

## 30 秒演示路径

1. `docker compose up --build`，打开 <http://localhost:8000/docs>。
2. `POST /api/demo/seed` → Execute（Compose 已自动执行过；该接口幂等）。
3. `POST /api/boxes` → 请求体已预填拦截牌（或在 Examples 下拉选择
   **demo_blocked_by_occluded_reading**）→ Execute → **409**，
   返回冲突箱号 `BOX-DEMO-01` 与相关读数 `RD-0001`。
4. `POST /api/scans` → 示例 **occluded_corners** → Execute：
   唯一命中 `BOX-DEMO-01`，确认编号 `RD-0001`。
5. 再 Execute 一次：`reused=true`、`pinned=true`，仍是 `RD-0001`，
   候选只有原箱。
6. 换 **valid_new_tag** 示例挂牌 → **201**（差异足够大，不冲突）。

也可以在宿主机跑无依赖脚本（Python 标准库）：

```bash
python3 scripts/walkthrough.py http://localhost:8000
python3 scripts/race_check.py  http://localhost:8000 16   # 同时挂牌+扫码
python3 scripts/fixture_walkthrough.py http://localhost:8000   # 开孔→采用→新牌撤下
```

## 开孔方案 API（单台扫码夹具）

现场勘测夹具在 5×5 点阵牌上最多挡住 **12** 个格位，并标出其中**允许开孔**的位置。
服务给出一组**开孔数最少**的方案，使开孔后最多剩 **4** 格被遮挡，并保证：

> 牌册中任一箱牌，在 **4 个放置朝向**下，对“剩余遮挡掩码 + 可见格 0 或 1 个错格”
> 的所有扫描，识别结果都**只有原箱**。

### 判定口径（为什么要求“可见差异 ≥ 3”）

对一对箱牌 `A`（某放置朝向）与 `B`（某匹配旋转），记 `d` 为剩余遮挡掩码之外、
两码仍不同的可见格数。一次“恰好错 1 格”的现场误差可以落在某个差异格上，把该差异
抵消一格，于是 `B` 能与某个合法错格扫描共匹配当且仅当 `d - 1 ≤ 1`，即 `d ≤ 2`。
因此严格满足“错 0 或 1 格都只命中原箱”的充要条件是 **`d ≥ 3`**（遍历所有箱对 ×
4 个放置朝向 × 4 个匹配旋转）。该判据已用独立的暴力枚举预言机
（逐箱、逐朝向、逐 0/1 翻格后跑真实 `match_grid`，300 组随机用例）核对一致。

- 默认 `mode="errors_0_1"`（上述严格口径）。请求传 `"mode":"matcher_only"`
  可改用较弱口径：只把掩码真值送 `match_grid`（≤1 容差），对应 `d ≥ 2`；
  评估响应里也附带 `matcher_only_plan` 的最少孔数，便于对照。
- **最少孔 = 让剩余遮挡格数尽可能多（≤4）且掩码安全**。不可开孔位 `U` 必须留在
  掩码内；枚举 `U ⊆ M ⊆ blocked`、`|M| = 4..|U|`，取第一个安全且最大的 `M`，
  开孔集 `H = blocked \\ M` 即最小。`C(12,4)=495` 量级，纯 Python 即时完成。
- 允许开孔的位置全开（最小可达掩码就是 `U`）仍无解时：
  - `|U| > 4` → `reason="occlusion_exceeded"`（遮挡超限）；
  - 否则 `reason="ambiguous_pair"`，返回至少一个 **会同时命中两个箱的 `witness_grid`**
    以及 `conflicting_box_numbers` / 两个箱号、朝向、差异格坐标。

### 生命周期与并发（规划不挡挂牌，采用必复核，新牌可原子撤下）

| 动作 | 接口 | 说明 |
| --- | --- | --- |
| 只算方案 | `POST /api/fixture/plans/evaluate` | 只读、不持全局锁，**规划期间照常挂牌**；返回 `feasible/minimum_holes/holes/remaining_blocked` 等 |
| 存提案 | `POST /api/fixture/plans` | 可行 **201** + `PLAN-0001`；无解 **409**（仍存 `infeasible` 记录，含冲突/格阵） |
| 采用 | `POST /api/fixture/plans/{id}/adopt` 或 `POST /api/fixture/plan/adopt` | 在 `BEGIN IMMEDIATE` 内**按当前牌册重新复核**；安全才置为唯一生效方案，旧方案转 `superseded`；不安全 **409 `plan_verification_failed`** 且**原生效方案保持不变**（`preserved_active_plan_no`） |
| 查询当前 | `GET /api/fixture/plan` | `active/active_plan` + 实时 `live_verification` + 顶层 `conflicting_box_numbers`；无生效方案时给出最近撤下方案/最新提案的冲突箱号 |
| 列表/详情 | `GET /api/fixture/plans` · `GET /api/fixture/plans/{id}`（另有 `/current /active /latest`） | 历史与冲突 |
| 手动撤下 | `POST /api/fixture/plan/withdraw` | 当前方案转 `withdrawn` |
| 一键勘测+采用 | `POST /api/fixture/plan`，体里 `"adopt": true` | 计算、存提案并复核生效 |

**合法新牌的原子撤下**：`POST /api/boxes` 通过既有全部挂牌冲突检查后，在**同一事务**
内复核当前生效方案；若新牌使其不再安全，则把方案置 `withdrawn`、清空单例指针，再与
箱牌一起提交。挂牌仍是 **201**，响应新增：

```json
"active_fixture_plan_withdrawn": true,
"fixture_plan": {
  "plan_no": "PLAN-FIX-01", "withdrawn": true,
  "reason": "new_tag_made_plan_ambiguous",
  "trigger_box_number": "FIX-BREAK",
  "conflicting_box_numbers": ["FIX-A", "FIX-BREAK"]
}
```

路径兼容别名（不在 `/docs` 展开）：`/api/plan`、`/api/plans`、`/api/hole-plan`、
`/api/drill-plan`、`/api/opening-plan`、`/api/fixture-plan`、`/api/fixture/survey`、
`/api/fixture/solve`、`/api/adopt`、`/api/withdraw` 等。位置支持 `0..24` 行主序整数、
`[row,col]`、`"r,c"`、`{row,col}`/`{position}`、`cells:[{position,openable}]` 描述，
以及 5×5 或长度 25 的掩码；输出同时给扁平下标、`*_coordinates` 与示意网格。

### `/docs` 演示：采用方案 → 被新牌撤下

`docker compose up --build` 时 `SEED_DEMO=1` 会**额外幂等执行**
`fixture_demo.seed_fixture_demo()`，建好 `FIX-A/FIX-C/FIX-D` 与提案 `PLAN-FIX-01`
（8 挡、6 可开；最少开 `[11,12,13,17]` 四孔，保留 `[1,6,7,8]` 遮挡，其中 6/8 不可开）。

1. 打开 `/docs`，`POST /api/fixture/plans/PLAN-FIX-01/adopt` → **200** `status=active`。
   （或 `POST /api/fixture/plan/adopt` 用示例 `adopt_demo_plan`。）
2. `GET /api/fixture/plan` → `active=true`、`conflicting_box_numbers=[]`。
3. `POST /api/boxes` 选示例 **fixture_break_withdraws_plan** → **201**，但
   `fixture_plan.withdrawn=true`、冲突箱号 `FIX-A` 与 `FIX-BREAK`。
   （`FIX-BREAK` 与 `FIX-A` 完整码仅差 2 格，属合法挂牌；差异格 6/8 恰在生效掩码内。）
4. 再 `GET /api/fixture/plan` → `active=false`、`last_withdrawn_plan.status=withdrawn`、
   `withdraw_reason=new_tag_made_plan_ambiguous`，顶层列出冲突箱号。
5. 无解示例：`POST /api/fixture/plans/evaluate` 选 **fixture_ambiguous_witness**
   （`reason=ambiguous_pair`，给 `witness_grid` 与 `FIX-C/FIX-D`）或
   **fixture_occlusion_exceeded**（5 个不可开孔位，`reason=occlusion_exceeded`）。

完整重播：`POST /api/demo/fixture-reset`（删除 `FIX-%` 箱/读数与全部方案、清空生效指针）
后再 `POST /api/demo/fixture-seed`。`scripts/fixture_walkthrough.py` 用纯标准库把上述
流程全部断言一遍。

## 规则如何落地

| 需求 | 实现 |
| --- | --- |
| 5×5 黑白码登记 | `POST /api/boxes`，单元格 `0/1`；完整码不允许遮挡 |
| 同尺寸格阵、最多 4 格看不清 | `POST /api/scans`；`null`（或 `?`/`x`/`-1`/`2`）表示看不清，>4 格返回 422 |
| 四旋转朝向 + 可见格至多错 1 格 | `app/codes.py::match_grid`，枚举 0/90/180/270°，只统计可见格误差 |
| 返回候选箱号 | 响应字段 `candidate_box_numbers`（同时给别名 `candidates`）与每个命中的 `orientation/mismatches` |
| 唯一箱号才确认并编号 | 仅当候选箱号集合大小为 1 时写 `readings`，编号 `RD-0001` 顺序生成 |
| 重扫同一格阵沿用编号 | `readings.grid_key`（25 字符，遮挡记 `?`）唯一；命中已有读数直接返回原编号，`reused=true` |
| 已确认读数始终只命中原箱 | 同格阵重扫走“钉住”记录，只返回其 `box_number`（`pinned=true`）；且冲突挂牌会被拒绝，库内不存在“读数同时命中别箱”的状态 |
| 新完整码命中旧箱 → 拒绝 | 挂牌前对牌册做同规则匹配，409 + `complete_code_matches_existing_box` |
| 任一已确认读数命中新牌 → 拒绝并给箱号/读数编号 | 分块索引召回 + 精确复核，409 + `confirmed_reading_matches_new_tag`，给 `conflicting_box_numbers` / `related_reading_ids` |
| 核查不能全扫已确认读数 | `reading_chunks` 索引表，6 次索引查询取候选，再按主键批量取回复核（见下） |
| 挂牌/扫码并发正确性 | 进程内 `RLock` 串行化检查+写入；每个操作单事务 `BEGIN IMMEDIATE`，SQLite 写锁兜底多进程；Compose 固定单 worker |
| 最多 12 个挡格、标可开孔 | `app/fixture.py::extract_survey`，多种线格式；>12 或可开孔位不在挡格内 → 422 `invalid_survey` |
| 最少开孔 + ≤4 剩余遮挡 + 四朝向 + 0/1 错格唯一 | `app/fixture.py::plan_minimum` / `evaluate_mask_detailed`：箱对×朝向距离判据 `d≥3`，枚举最大安全剩余掩码；独立暴力预言机核对 |
| 全开仍无解 | `reason=occlusion_exceeded`（不可开孔位 >4）或 `ambiguous_pair`（给共命中 `witness_grid` 与 `conflicting_box_numbers`） |
| 采用时按当前牌册复核、失败保原状 | `plans_service.adopt_plan` 在锁 + `BEGIN IMMEDIATE` 内重算；409 `plan_verification_failed` + `preserved_active_plan_no`，不动单例指针 |
| 合法新牌致生效方案歧义 → 挂牌+撤下同生效 | `services.register_box` 插箱后在**同一事务**调 `review_active_plan_in_tx`；201 响应带 `fixture_plan.withdrawn` |
| 查询方案列冲突箱号 | `GET /api/fixture/plan` 顶层 `conflicting_box_numbers/conflicts`，并给实时 `live_verification` |

### “不能全扫已确认读数”的索引原理

25 格按行主序切成 **6 个互不相交的块**（5 块×4 格 + 1 块×5 格）。
一次命中最多有 `4 个遮挡格 + 1 个错误格 = 5` 个“坏格”。5 个坏格放进 6 个块，
由抽屉原理，**至少一个块完全干净**：该块在读数中全部可见且与新牌逐格相同。

因此每条确认读数写入时，把它 **4 个旋转朝向**下每个“全可见块”的精确比特签名
写入 `reading_chunks(chunk_index, bits, reading_id)`；挂牌时对新牌的 6 个块做
索引查询即可召回所有潜在冲突读数，再用完整匹配器排除假阳性。

- 召回保证：4000 组随机用例 + 对抗性布局测试验证“零漏报”；
- `EXPLAIN QUERY PLAN` 显示块查询走主键索引 `SEARCH ... USING PRIMARY KEY`；
- 挂牌链路不调用 `repo.list_readings`（有 monkeypatch 测试），
  取行只走 `WHERE id IN (...)` 主键批量取；
- 1200 条读数规模下，索引结果与测试内暴力全扫预言机完全一致。

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/boxes` | 登记箱牌；可给 `box_number`，省略则分配 `BX-0001`；冲突 409 |
| `GET  /api/boxes` / `GET /api/boxes/{box_number}` | 牌册查询 |
| `POST /api/scans` | 扫码识别；返回候选、确认状态、读数编号 |
| `GET  /api/readings` / `GET /api/readings/{RD-0001或数字id}` | 确认读数 |
| `POST /api/demo/seed` / `GET /api/demo/scenario` | 演示数据与现场脚本 |
| `POST /api/fixture/plans/evaluate` | 提交挡格/可开孔位，返回最少孔方案；无解给原因、共命中格阵、冲突箱号 |
| `POST /api/fixture/plans` · `GET /api/fixture/plans/{id}` · `GET /api/fixture/plans` | 方案提案（可行 201/无解 409）、详情、历史 |
| `POST /api/fixture/plans/{id}/adopt` · `POST /api/fixture/plan/adopt` | 按当前牌册复核并生效；失败 409 且保留原状态 |
| `GET /api/fixture/plan` · `POST /api/fixture/plan/withdraw` | 当前方案 + 实时复核 + 冲突箱号；手动撤下 |
| `POST /api/demo/fixture-seed` / `fixture-reset` / `GET /api/demo/fixture-scenario` | 开孔 `/docs` 演示数据、重置与脚本 |
| `GET /health` | 健康检查 + 索引计数 |
| `GET /docs` | Swagger UI（静态资源已自托管，不依赖 CDN） |

开孔方案的路径别名（不在 `/docs` 展示）：`/api/plan`、`/api/plans`、
`/api/hole-plan`、`/api/drill-plan`、`/api/opening-plan`、`/api/fixture-plan`、
`/api/fixture/survey`、`/api/fixture/solve`、`/api/adopt`、`/api/withdraw` 等。
勘测字段别名：`blocked/blocked_positions/positions/occluded/covered/mask` 与
`openable/drillable/can_open/allowed_holes`，也接受 `cells:[{position,openable}]`。

兼容别名（不在 `/docs` 展示）：`/boxes`、`/register`、`/scans`、`/scan`、
`/recognize`、`/api/register`、`/api/recognize`。
字段别名：箱号接受 `box_number/boxNumber/box_no`，格阵接受
`grid/code/matrix/cells/pattern`。格阵除标准 5×5 数组外，也接受一维 25 格、
5 个字符串行等常见扫描器格式。

## 本地开发与测试

```bash
python3 -m pip install -r requirements-dev.txt
DB_PATH=data/exhibit.db SEED_DEMO=1 \
  python3 -m uvicorn app.main:app --reload --port 8000
python3 -m pytest          # 49 个用例：规则/API/索引/并发/开孔方案与竞速
```

SQLite 文件位置由 `DB_PATH` 决定（Compose 中为 `/data/exhibit.db` 命名卷）。

## 目录结构

```
app/
  codes.py          # 格阵解析、旋转、匹配、分块签名（纯逻辑）
  fixture.py        # 开孔勘测解析、距离判据、最少孔枚举、共命中格阵（纯逻辑）
  db.py             # SQLite schema（含 fixture_plans/fixture_state）、BEGIN IMMEDIATE
  locking.py        # 进程内全局 RLock（规划只读路径不取锁）
  repository.py     # 牌册/读数/块索引/方案与单例状态的数据访问
  services.py       # 挂牌、扫码、冲突核查、编号钉住；挂牌事务内联动撤下方案
  plans_service.py  # 方案评估/提案/采用复核/撤下/查询/挂牌事务钩子
  fixture_api.py    # 开孔方案与演示的 FastAPI 路由
  schemas.py        # pydantic 模型与 /docs 示例（含开孔示例）
  demo.py           # 遮挡拦截演示数据
  fixture_demo.py   # 开孔：PLAN-FIX-01 / FIX-A,C,D,BREAK 与重置
  main.py           # FastAPI 路由、自托管 Swagger、OpenAPI 示例、路径别名
  static/vendor/    # 本地 swagger-ui js/css
scripts/
  walkthrough.py          # 纯标准库端到端演示（旧场景）
  fixture_walkthrough.py  # 纯标准库 开孔→采用→新牌撤下 演示
  race_check.py           # 纯标准库并发挂牌/扫码校验
tests/              # 49 个 pytest 用例（含开孔预言机、API 生命周期、竞速）
Dockerfile docker-compose.yml
```

## 实测结果（本次收尾核对）

- `python3 -m pytest`：**49 passed**。原 **29** 个用例（4000 组随机索引完备性、
  1200 条读数暴力预言机比对、真实 uvicorn + 14 线程 HTTP 同时挂牌/扫码）全部保持
  通过；新增 **20** 个开孔用例：
  - 距离判据 vs 独立暴力枚举预言机（300 组随机牌册/掩码，逐朝向逐 0/1 翻格）一致；
  - 200 组随机勘测下，返回方案与全掩码穷举的最小孔数/可行性完全一致；
  - `occlusion_exceeded` 与 `ambiguous_pair` 见证格阵确实同时 `match_grid` 命中两箱；
  - API：最少孔、422 校验、201/409、采用复核失败保留 `PLAN-KEEP` 原状态、
    `/docs` 演示流的“201 挂牌 + 同事务撤下 + 查询冲突箱号”、幂等 seed/reset、别名；
  - 10 线程 adopt×breaking-registration 竞速后状态始终一致（无 5xx、无“生效但不安全”）。
- 真实 `uvicorn app.main:app --workers 1`（临时 `DB_PATH` + `SEED_DEMO=1`，即
  Compose 的等价启动命令）实测：启动健康检查 `boxes=4, confirmed_readings=1`
  （旧演示箱 + `FIX-A/C/D`，说明开孔演示数据随 `SEED_DEMO` 自动幂等写入）。
- `scripts/fixture_walkthrough.py` 对真实 HTTP 进程全部断言通过：
  勘测 → `minimum_holes=4 holes=[11,12,13,17] remaining=[1,6,7,8]`；
  adopt 200 `active`；登记 `FIX-BREAK` **201** 且
  `withdrawn=True ['FIX-A','FIX-BREAK']`；`GET /api/fixture/plan` 为
  `active=false` / `new_tag_made_plan_ambiguous` 且列冲突箱号；
  见证勘测 `ambiguous_pair`（含 `FIX-C/FIX-D`、`visible_differences=1` 与
  `witness_grid`）；超限勘测 `occlusion_exceeded non_openable=5`。
- `scripts/walkthrough.py`（旧拦截场景）在同一真实进程仍全部断言通过：
  拦截牌 **409** `['BOX-DEMO-01'] / ['RD-0001']`，重扫 `reused=pinned=True`。
- `/docs` **200**，自托管 JS/CSS **200**；`openapi.json` 正常生成，
  开孔勘测示例 4 个、采用示例 1 个，`/api/boxes` 下拉含
  `fixture_break_withdraws_plan`，文档化路径 19 条（另有一批隐藏兼容别名）。
- `docker-compose.yml` 通过 PyYAML 解析校验；无新增第三方依赖，镜像内
  `uvicorn app.main:app` 启动路径已用本地同版本依赖实测。

## 已实现功能清单（开孔需求逐条对照）

1. **开孔方案 API**：`app/fixture.py`（纯逻辑）+ `app/plans_service.py` +
   `app/fixture_api.py` + `fixture_plans/fixture_state` 两张表。
2. **最多 12 个挡格 + 标出可开位**：多种提交格式与别名字段，越界/超量 422。
3. **最少开孔方案**：枚举最大安全剩余掩码给出最小孔集，附备选与坐标/示意网格。
4. **≤4 剩余遮挡 + 四朝向 + 0/1 错格只命中原箱**：`d≥3` 判据并经暴力预言机核对。
5. **全开仍无解的两类交代**：遮挡超限，或给共命中格阵 + 两个箱号（含朝向/差异坐标）。
6. **规划期间照常挂牌**：评估只读且不取全局锁；挂牌接口与原行为不变。
7. **采用时按当前牌册复核，安全才生效，失败保留原状态**：锁 + `BEGIN IMMEDIATE`，
   409 带 `preserved_active_plan_no`。
8. **合法新牌致生效方案歧义时，挂牌与撤下一并生效**：同一注册事务内撤下并提交。
9. **查询方案列出冲突箱号**：`GET /api/fixture/plan` 顶层与详情均给出。
10. **Docker Compose + `/docs` 演示撤下过程**：Compose 复用 `SEED_DEMO=1`
    自动备料，`/docs` 有预填示例与落地页步骤，另附纯标准库演练脚本。

## 已知限制 / 待完成事项

- 本构建沙箱**没有安装 Docker/Podman，也没有 Docker socket/守护进程或 root/sudo**，
  因此 `docker compose up --build` 未能在此实际执行；已用 Compose 的等价命令
  （同 `requirements.txt` 锁定依赖、单 worker、`SEED_DEMO=1`、临时文件库）真实起服
  并跑通两个 walkthrough 与全部 pytest。`docker-compose.yml` 已通过 YAML 校验。
  **待在有 Docker 的环境补一次 `docker compose up --build` 与 `/docs` 点击确认。**
- 安全口径默认采用较强的“枚举所有 0/1 错格现场扫描后仍唯一”（箱对可见汉明距离
  `d≥3`）。若评审本意只是“掩码真值过一遍 `match_grid` 的 ≤1 容差”（`d≥2`），
  请在请求中传 `"mode":"matcher_only"`；评估响应也给了 `matcher_only_plan` 对照。
  两种口径下，本交付的 `/docs` 演示数据（差异被掩码完全遮住，`d=0`）都会稳定触发。
- 开孔方案按“**单台**夹具”建模为单例生效状态（`fixture_state` 仅 id=1 一行）；
  多台夹具需要把该表改为按 `fixture_id` 持有多行并在勘测/采用时带夹具标识。
- 演示重置接口 `POST /api/demo/fixture-reset` 会物理删除所有 `FIX-%` 箱牌及其读数、
  以及**全部**方案记录，仅用于演示重播；没有提供通用箱牌删除接口。
- 为保证“检查+写入”串行，Compose 固定 **1 个 uvicorn worker**；多 worker
  部署下正确性由 `BEGIN IMMEDIATE` 事务兜底，但进程内锁不再跨进程，
  高并发时可能出现 `database is locked`（当前 busy_timeout=15s）。
- 无鉴权/多租户/审计日志；读数列表默认返回最近 200 条
  （`?limit=` 可调，上限 10000），方案列表默认 100 条。
- 只实现了需求中的四旋转，不包含镜像翻转。

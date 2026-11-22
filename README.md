# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布、查询与到站准点核算网页。数据工程师一次性上传
同一版本的 5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才
发布；公众按「线路 + 运营日」查询有序停站时刻；调度员可选定运行图、线路、运营日和当天一趟
班次，上传实到 CSV 进行停站对齐与准点核算。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **准点核算**：选定已发布运行图、线路、运营日和当天一趟班次，上传或整份更正实到 CSV；
  网页列出每条已定位记录的停站序号与时差，标出待核行与候选序号，并按站展示计入条数和准点率。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）和 `sample_data/invalid/`（含典型错误）
两组样例数据在网页上直接试；`sample_data/actual/` 提供实到核算样例：

| 文件 | 说明 |
|---|---|
| `N1_actual_good.csv` | 跳过未报站后全部唯一定位，时差在 ±5 分钟内为准点 |
| `N2_actual_pending.csv` | 单条记录同时匹配环线上两个同站序号，存在歧义 → 待核 |
| `N2_actual_infeasible.csv` | 含计划中不存在的站号，无可行对齐 → 整趟待核 |
| `actual_invalid.csv` | 实到时刻重复 + 时刻格式错误，整份拒绝并保留上次记录 |

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。

## 实到准点核算规则

### 实到 CSV 格式

表头必备 `stop_id,actual_time` 两列（允许额外列；`stop` / `actual` 等别名也可识别）：

```csv
stop_id,actual_time
S1,23:31:00
S3,24:21:00
```

- 每行填站号和实到时刻；跨午夜沿用 `24:00` 以上的 GTFS 写法（如 `25:10:00`）；
- **同趟实到时刻不得重复**；至少一行数据；
- 格式错误（缺列、坏时刻、空字段、重复时刻、列数不符）整份拒绝（422），
  返回全部错误行，**已保存的上次记录和核算结果保持不变**。

### 对齐算法（环线同站多次经过）

记录按实到先后与按 `stop_sequence` 排序的计划停站对齐：

1. 允许跳过未报站的计划停站，但**停站序号必须严格递增**；
2. 记录站号必须与计划停站站号相同，且实到与计划到站**相差不超过 30 分钟**；
3. 系统对全部记录做前向贪心（取最早可行序号）与后向贪心（取最晚可行序号）：
   一条记录在两次贪心结果中落到**同一停站序号**，才表示它在所有可行对齐中位置唯一，
   状态为 `located`（已定位）并计入统计；否则状态为 `pending`（待核），网页标出其候选序号；
4. 不存在覆盖全部记录的可行对齐时（站号对不上、全部超出 30 分钟窗口、序号无法递增等），
   **整趟所有记录均为待核**，不计入任何统计。

### 统计口径

- 时差 = 实到时刻 − 计划到站时刻（正为晚点、负为早到），单位分钟；
- 时差**绝对值不超过 5 分钟**算准点；
- 按站汇总**已定位**记录的计入条数、准点条数与准点率；待核行一律不计入；
- 明细与统计读取同一份已保存数据（`actual_records` 表）。

### 更正与并发语义

- 一趟班次由 `(版本, trip_id, 运营日)` 唯一标识；再次上传即**单事务整份替换**，
  保存失败自动回滚、旧数据保留；
- 实到数据绑定发布时的版本 ID，历史版本的更正不会与新版本串读；
- 不同班次的并发更正各自独立生效（SQLite WAL + 行级唯一键）。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes?version_id=` | 线路列表（可指定历史发布版本，默认当前版） |
| GET | `/api/routes/{route_id}/schedule?date=&version_id=` | 该运营日的班次与有序停站时刻 |
| POST | `/api/actual-arrivals` | multipart 上传实到 CSV（version_id/route_id/service_date/trip_id + file），保存对齐与核算结果；格式错误 422 且保留上次记录 |
| GET | `/api/actual-arrivals?version_id=&route_id=&service_date=` | 已更正班次列表 |
| GET | `/api/actual-arrivals/stats?version_id=&route_id=&service_date=` | 按站计入条数与准点率（仅已定位记录） |
| GET | `/api/actual-arrivals/{upload_id}` | 单趟明细：停站序号、时差、准点标志、待核候选序号 |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"

# 上传实到 CSV 并核算（选中当前正式版的 R2/N1 班次、运营日 2026-11-23）
curl -F "route_id=R2" -F "service_date=2026-11-23" -F "trip_id=N1" \
     -F "file=@sample_data/actual/N1_actual_good.csv" \
     http://localhost:8000/api/actual-arrivals

# 按站准点统计
curl "http://localhost:8000/api/actual-arrivals/stats?route_id=R2"
```

## 目录结构

```
app/
  main.py          FastAPI 端点（发布 / 查询 / 版本 / 实到准点核算）
  gtfs.py          时刻表 CSV 解析与整版校验（纯标准库）
  actual.py        实到 CSV 解析校验与停站对齐算法（纯标准库）
  db.py            SQLite schema、原子发布、版本指针、实到整份替换与统计
  static/index.html  网页（查询 / 准点核算 / 发布 / 版本记录）
tests/             pytest 单元与集成测试
sample_data/       可通过与含错误的时刻表样例、实到核算样例
Dockerfile / docker-compose.yml
```

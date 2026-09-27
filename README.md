# 急救车调度与目的地分流

纯Python标准库实现的急救车调度与目的地分流原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、优先级评分、能力匹配、目的地分流选择、车辆冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务、占床和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、目的地分流、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表。

## 目的地分流规则

1. 医院目录维护每家医院的救治能力（`BLS`/`ALS`）、总床位、可用床位和车程分钟数。
2. 接单（创建任务）时先按能力筛选：病人所需能力不超过医院能力（ALS医院可收BLS病人）。
3. 在车程20分钟以内且有可用床位的医院中选择车程最快的一家，并在同一事务内原子占床。
4. 没有具备能力的医院、20分钟窗口内全部满床或全部超出车程时拒绝接单（`422 no_destination`），不产生任务记录。
5. 两个任务并发抢最后一张床时，条件更新保证只有一个成功；失败方自动改选下一家合格医院，没有备选才拒绝。
6. 任务取消释放床位（`bed_status=released`），交接完成核销床位（`bed_status=consumed`）。
7. 任务详情（`payload.destination`、`destination_hospital_id`、`destination_drive_minutes`、`bed_status`、`reroute_reason`）和审计时间线（`created`/`cancel`/`handover`事件）都会记录医院、占床结果和改选原因。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录（接单并自动分流占床），请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/hospitals`：医院目录列表。
- `POST /api/hospitals`：新增医院，请求体为`{"code":"H1","name":"...","capability":"ALS","total_beds":4,"drive_minutes":10}`。
- `PUT /api/hospitals/{id}`：更新医院能力、总床位、车程或启停状态。
- `POST /api/hospitals/{id}/beds`：人工调整可用床位，请求体为`{"delta":-1}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。医院目录维护仅`dispatcher`、`hospital_coordinator`和`admin`可用。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、目的地分流（能力筛选、20分钟窗口、占床、释放、并发抢床改选）、规则计算、重复引用、权限拒绝和版本冲突。

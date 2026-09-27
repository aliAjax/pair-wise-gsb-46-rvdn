# 急救车调度与目的地分流

纯Python标准库实现的急救车调度与目的地分流原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、优先级评分、能力匹配、车辆冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，payload内含目的地医院、占床与改选原因。
- `GET /api/records/{id}/audit`：审计时间线，含占床、改选与拒单事件。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/hospitals`：医院目录列表（按车程升序）。
- `GET /api/hospitals/{id}`：医院详情。
- `POST /api/hospitals`：新增医院，请求体为`{"data":{"name":"...","capabilities":["BLS","ALS"],"total_beds":4,"drive_minutes":12}}`，`available_beds`缺省等于`total_beds`。
- `POST /api/hospitals/{id}/update`：维护医院能力、床位与车程，请求体为`{"data":{...}}`。

医院目录仅`admin`和`hospital_coordinator`可写，其余已知角色可读。

## 目的地分流规则

- 执行`assign`时由服务端选院：先按救治能力筛选（ALS医院可覆盖BLS需求），再在20分钟车程内选有空床且车程最快的一家，原子占床后写入任务。
- 两个任务同时抢最后一张床时只有一单占床成功，另一单自动改选下一家合格医院；无院可选时`assign`返回409拒绝，任务保持`received`，并写入`assign_rejected`审计事件。
- 更快的医院因满床被跳过、或占床瞬间被抢，都会作为`reselect_reasons`记入任务payload和审计。
- `cancel`释放已占床位（`released_bed`记入审计）；`handover`后床位视为医院实际收治，由医院协调员通过目录接口维护床位数。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

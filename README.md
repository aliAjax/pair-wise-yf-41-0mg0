# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本；`report`：独立的台站报文。

### 台站报文（report）

报文独立成记录，不再嵌在事件里。每份报文有唯一编号 `code`（如 `RP-000001`）、观测时刻 `observed_at`、台站 `station`、振幅 `amplitude`，以及归属事件 `event_id`。一份报文只归一个事件；`event_id` 为空表示未归属。

- 创建报文：`POST /api/reports`，`{"station":"S1","observed_at":"...","event_id":"<event_id>","amplitude":2.0}`。`event_id` 可省略（先不归属）。
- 改归属：`POST /api/entities/<report_id>/actions`，`{"action":"assign","data":{"event_id":"<event_id>"},"expected_version":N}`。

改归属后会立刻重算**源事件和目标事件**的台站数 `station_count` 与震级 `magnitude`（震级取所属报文振幅的中位数）。若事件已发布（`published`/`revised`），改归属会把状态退回 `reviewed`（待复核），而发布时留存的外发内容 `published_snapshot` 保持旧版不变。

### 并发归属：先到生效

两名分析员同时提交同一份报文的归属，先到的生效（版本号 +1）。后到的若带旧版本号会收到 `409 Conflict`，响应体的 `details` 里会带出对方已占的归属：

```json
{"error":"version conflict: expected 1, found 2","type":"ConflictError",
 "details":{"report_code":"RP-000001","current_event_id":"<对方占的事件id>","current_version":2}}
```

### 批量导入与失败重试

`POST /api/batch`，请求体 `{"kind":"report","items":[{"ref":"r1","data":{...}},...]}`，配合 `Idempotency-Key` 头作为批次号。逐条写入并记录每条 `ref` 的结果（`created`/`skipped`/`failed`）。若某条失败，重试同一批次号时只补写没写进的，已成功的跳过，不会重复。

### 旧数据迁移

服务启动时自动把事件内嵌的 `reports` 迁移成独立的 `report` 记录（编号、观测时刻、归属事件齐全），并移除事件里的内嵌报文、重算台站数。迁移幂等，可重复启动。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`kind` 含 `station`/`event`/`report`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `POST /api/batch`：批量导入，配合 `Idempotency-Key` 批次号，失败重试只补未写进的。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。报文用 `assign` 改归属；事件用 `associate`/`review`/`publish`/`revise`/`withdraw`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。

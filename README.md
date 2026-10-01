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

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `report`：独立台站报文记录，含唯一编号 `code`、观测时刻 `observed_at`、台站 `station` 和归属事件 `event_id`。一份报文只归属一个事件。

### 报文归属与事件重算

- `POST /api/entities/<report-id>/actions` 提交 `{"action":"reassign","data":{"event_id":"..."},"expected_version":N}` 可把报文移到另一事件。
- 归属改动在单个数据库事务内完成，并立即重算原事件和新事件的 `station_count`（去重台站数）和 `magnitude`（报文震级中位数）。
- 改归属必须携带分析员决策时看到的 `expected_version` 作为乐观锁围栏：两名分析员同时提交同一份报文时先到者生效，后到者收到 `409 Conflict`，响应 `details` 含报文编号、当前归属事件、当前版本和先占用者 `claimed_by`。
- 已发布（`published`）事件因归属变化被改动时自动退回 `pending_review`，需重新复核。

### 外发快照

- 发布时把当时的事件数据与全部归属报文冻结到 `publications` 表；之后的改归属、重算、退回都不会改动已外发内容。
- `GET /api/publications`、`GET /api/publications/<event-id>` 读取外发版本。

### 批量导入与旧数据迁移

- `POST /api/reports/import`，请求体 `{"reports":[...]}`：逐行独立入库，失败行计入 `failed` 且不阻断其他行；按 `code` 已存在的行计入 `skipped`，因此失败后重试只会补写没写进去的报文，导入后自动重算涉及事件。
- 服务启动时自动把旧事件 `data.reports` 内嵌的报文迁移成独立 `report` 记录（观测时刻由事件发震时刻加 `time_offset` 推导）。迁移幂等，重复启动不会重复建记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；`report` 还支持 `?event_id=` 按归属事件过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。

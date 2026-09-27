# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；登记时可填`source_batch_id`（来源批次）和`receiving_facility_id`（接收设施），两者都会校验引用存在。
- `facility`：温室、苗圃或下游种植点。
- `review`：传播复核记录；确认虫害后自动为下游批次和相关设施创建。

## 传播追溯流程

1. 批次执行`quarantine`（确认虫害）后，系统按`source_batch_id`逐级（BFS）找出所有下游批次。
2. 下游批次及其链上所有接收设施自动创建`review`（状态`pending`），目标对象进入`under_review`并记录原状态；已有待复核记录的对象不会重复创建。
3. 复核动作：`confirm`后批次进入`quarantined`、设施进入`locked`，并继续追溯该批次下游；`exclude`后目标恢复复核前状态。
4. `GET /api/trace/<consignment_id>`返回整条传播关系（含层级深度）、各对象状态、复核记录和处理进度统计；`POST /api/trace/<consignment_id>`可对已隔离批次手动重新触发追溯。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/trace/<id>`：查询传播链与复核进度。
- `POST /api/trace/<id>`：手动触发追溯（要求批次已隔离）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。

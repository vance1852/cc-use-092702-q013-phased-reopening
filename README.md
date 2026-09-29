# 编排灾后分阶段恢复开放基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/recovery_governance/：灾后按受影响区域编排的分阶段恢复指令、证据版本、复核责任、生效期、收紧与审计重建；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m recovery_governance.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及灾后分区域分阶段恢复（前置不齐放行被拒、证据失效与到期收紧、幂等回放、公众视图与审计重建），不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m recovery_governance.api --database recovery.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

灾后恢复治理服务（端口 8083）要点：

- 协调员（`X-Actor-Id: coord`）登记受影响区域（巡护道路、游客步道、科研样地、住宿区）、提交分阶段计划并发布；每阶段固定依赖的证据版本、复核责任人（复核员或协调员）、最低通过项与生效小时数，前置阶段未生效或证据未逐项复核时激活被拒，不会扩大业务范围。
- `POST /stages/{id}/activate` 需带 `Idempotency-Key`：重复执行回放原决定并记录 `replayed` 历史；指令有生效期，`POST /sweep` 或任何读取投影的操作会自动把到期、证据失效、计划撤回、前置失效的指令收紧（后续阶段级联失效）。
- 巡护、科研、游客、经营四类接口统一读取 `GET /channels/{channel}/projection` 与 `GET /areas/{id}/projection`，同一阶段投影无法各自提前放行；现场执行经 `POST /activations/{id}/executions` 留痕，指令收紧后立即拒绝。
- `GET /public/status` 无需登录，只返回各区域必要的开放状态（关闭/受控开放/开放）；审计员经 `GET /activations/{id}/record` 重建每次决定的证据版本摘要、复核签字、收紧时间线与现场执行，`GET /audit/chain` 校验哈希链。

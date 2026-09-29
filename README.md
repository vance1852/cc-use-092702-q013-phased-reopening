# 编排灾后分阶段恢复开放基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/recovery_ops/：灾后分阶段恢复编排（恢复计划、证据版本、阶段复核、恢复指令、阶段投影与审计重建）；
- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 灾后分阶段恢复编排

联席会按受影响区域编排恢复计划，每个阶段声明依赖的证据版本、复核责任角色、
最低通过项以及巡护、科研、游客、经营四类业务的开放上限。恢复指令必须带生效期，
签发时把证据版本、复核记录和前置通过项快照进决定依据；前置条件不齐时不得扩大
任何业务范围。重复执行、撤回、到期或证据失效都会写入哈希链审计历史并立即收紧
权限。四类业务接口读取同一阶段投影，公众接口只暴露必要的开放状态，审计人员可
重建每次决定依据以及现场实际执行进度。

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
PYTHONPATH=src python3 -m recovery_ops.acceptance --workspace .
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
~~~

四条命令会在临时 SQLite 数据库中完成分阶段恢复编排、保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m recovery_ops.api --database recovery.sqlite3 --host 127.0.0.1 --port 8083
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

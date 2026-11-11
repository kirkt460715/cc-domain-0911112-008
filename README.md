# researchmesh 数据接入工作台

保留现有领域校验、持久化与操作入口。Python 3.11 标准库和 SQLite；运行时无需下载依赖。测试覆盖已有领域行为、错误路径和恢复。本地联调材料不会自动采集到业务库。

## 本地运行

    python3 -m compileall -q researchmesh
    python3 -m unittest discover -s tests -v
    python3 -m researchmesh demo
    python3 -m synthetic_exchange.smoke

## 容器运行

    docker build -t local/researchmesh .
    docker run --rm --network none local/researchmesh

构建时准备基础镜像，正式运行不下载。run_checks.sh 是完整的测试、编译检查及实际产品演示入口，正常在 90 秒内结束。每次演示使用临时数据或内存库，不改写源样本。

## 采集联调

GET /oai?verb=ListRecords；后续附 resumptionToken=next。命名空间遵循 OAI-PMH 外层，metadata 的 m:record 为本地成果映射。identifier 可能包含冒号，需要稳定映射，deleted header 没有 metadata。日期粒度为天。

    python3 -m synthetic_exchange.server --port 8765

原有操作参数可通过 python3 -m researchmesh --help 查询。

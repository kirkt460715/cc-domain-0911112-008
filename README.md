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

## OAI-PMH 成果采集

`harvest` 子系统把 OAI-PMH 风格接口接入现有来源记录与字段清洗，无需手工拼 JSON：

    python3 -m researchmesh --db mesh.sqlite3 harvest lab h1 \
        "http://127.0.0.1:8765/oai" full
    # 增量：省略 --from 时自动沿用上一轮成功落库的天级水印
    python3 -m researchmesh --db mesh.sqlite3 harvest lab h2 \
        "http://127.0.0.1:8765/oai" incremental --from 2026-11-10
    # 只采集暂存、先核对 explain，再决定是否落库
    python3 -m researchmesh --db mesh.sqlite3 harvest lab h3 \
        "http://127.0.0.1:8765/oai" full --stage-only

关键安全语义，对应全量盘点“只拉到半页”的风险：

- **命名空间与分页**：按 OAI-PMH 命名空间名（而非前缀）解析 `ListRecords`，
  支持默认命名空间与显式前缀；逐页跟随 `resumptionToken`，空 token 表示遍历完成。
- **稳定 identifier 映射**：外部 identifier（可含冒号）经确定性 SHA-256 映射为
  本地记录 id，同一外部标识永远落到同一来源记录，再走现有 `source_contract` 清洗。
- **deleted header**：删除标记没有 metadata，落库时仅对已映射的活动记录生成
  `withdraw` 变更键；未映射的重复删除被记录并忽略。
- **完整快照的严格条件**：只有 `full` 模式、完整遍历、且从未中途失败/重采/
  generation 漂移的 run 才形成完整来源快照（缺席项才会被撤回）。解析错误或
  中途失败时 run 标记为 `partial`，已采页独立事务保留，缺席项绝不撤回；
  续采完成后也只按明确变更键处理。
- **固定时间边界**：每个 run 启动时固定 `from/until`（天精度）。token 过期
  （badResumptionToken）后从同一边界重采，不悄悄更换截止点；同一死 token 只
  重采一次，重复页按摘要去重，多版本按页序取最后版本，均可通过 `harvest.explain` 解释。
- **增量水印与重叠**：增量只发明确 `upsert/withdraw` 变更键；天级边界采用
  OAI 的闭区间语义，重叠页与重复 identifier 被对账而非重复落库。
- **generation 漂移保护**：采集期间来源配置或并发人工导入推进 generation 时，
  `harvest.apply` 拒绝写入并保留采集结果；需显式 `harvest.rebase` 确认后才能
  重新处理，且 rebase 后只走变更键，不覆盖人工提交。

对应的程序化动作（`harvest.start/collect/get/list_runs/explain/apply/rebase/watermark`）
与其它操作一样通过 `run` 请求分发，并写入同一审计链。

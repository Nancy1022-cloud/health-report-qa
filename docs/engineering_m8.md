# 健康报告 M8：真实现状、职责和后续验收用例

本文件描述提供的 M8 改动；本机执行结果以 `app/data/m8/v1` 为准。

## 1. 现状核对

参考源码：`Nancy1022-cloud/health-report-qa`，提交 `fe9a983a40398a16895006a4438fdcb0b2aebf4e`。
安装前用内容哈希检查本机代码，忽略 CRLF 与 UTF-8 BOM。检测到未知修改时停止覆盖。

| 项目 | 仓库已存在的实现 | 本机核对证据 |
| --- | --- | --- |
| 数据库 | psycopg + PostgreSQL；Compose 镜像为 PostgreSQL 16 的 pgvector 发行镜像 | before/inventory.json 中 server、extensions |
| 向量检索 | PostgreSQL pgvector `<=>` 余弦距离；片段保存模型、维度、版本和正文摘要 | inventory.json 的 vector_configs、indexes |
| 关键词检索 | jieba 分词、tsvector、ts_rank_cd、GIN；RRF 融合 | retrieval.py、text_search.py、rrf.py |
| 部署 | db + app 两服务，数据库命名卷；uploads 目录挂载 | compose.yaml |
| 草稿 | 原件与首次抽取存于 uploads 中的 source 文件和 draft.json | repositories/drafts.py |
| 确认 | 先完成 Embedding，再在同一数据库事务内写报告、指标和两种版本片段 | services/reports.py、repositories/reports.py |
| 重放与冲突 | 同一个上传 ID + 同样核对内容返回 already_confirmed；不同内容返回 409 | 两轮 behavior.json |
| 并发保护 | M6 已有 pg_advisory_xact_lock；锁内再次检查确认内容 | repositories/reports.py；实际并发验收留待 M10 |
| 模型 | .env 的实际配置；本轮不换模型或维度 | inventory.json 的 models，仅记录模型、主机和 Key 是否存在 |
| 历史检查 | 仓库 M5/M6/M7 unittest 合计 70 个测试方法 | before/unit_checks.txt；不得写成 175 个 |

技术介绍应使用 PostgreSQL、pgvector 和 PostgreSQL 全文检索。当前关键词排名不是 BM25。
运行版本可能与镜像标签不同，应以 `inventory.json` 的数据库查询结果为准。
`/health` 中原有 `stage: M2` 是历史字符串，不是当前里程碑验收结果。

## 2. 模块职责

| 层 | 文件 | 负责什么 |
| --- | --- | --- |
| Router | record_routes.py、query_routes.py、agent_routes.py、upload_routes.py | HTTP 参数、依赖声明、文件响应、旧错误形状映射 |
| Schema | schemas/queries.py、既有 upload_models.py | 原请求格式与核对校验；本轮只迁移已有规则 |
| Service | services/records.py | 成员存在性、报告范围、结果组装 |
| Service | services/reports.py | 上传与确认编排、重放/冲突判断、外部处理和写事务的先后次序 |
| Service | services/queries.py | 将问答请求交给已有 RAG/工具问答管道 |
| Repository | repositories/records.py | 列表、指标、索引统计、日志读取 SQL |
| Repository | repositories/reports.py | 报告读取、事务锁、报告/指标/片段写入 |
| Repository | repositories/drafts.py | 原件、摘要、草稿的文件读写与归属检查 |
| Client/处理适配 | clients/report_processing.py | M6 索引准备流程，Embedding/config 由调用方注入 |
| 既有外部适配 | report_extract.py、llm.py、rag.py 的模型函数 | 复用文件识别、模型配置和已验证的 SDK 调用 |
| 组合根 | dependencies.py、report_ingest.py 的 make_report_service | 将真实连接、存储和处理函数组装为服务；测试可提供替身 |
| 兼容入口 | report_ingest.py | 为旧 CLI/M6 检查保留函数入口；不保留第二套确认实现 |

迁移到 Service 的业务代码没有 FastAPI import。`RecordService` 与 `ReportService` 可直接构造后验证规则。
数据连接在依赖或 Repository 上下文结束时关闭；外部模型等待不放入报告写事务。
问答内部的 `agent.py`、`agent_tools.py`、`retrieval.py` 继续作为已有管道使用，其内部尚未全部完成 Repository 化。
这是一轮核心入口分层，不宣称整个仓库已无旧式依赖。完整响应模型、统一异常与 request_id 属于 M9。

## 3. 一次确认的顺序

1. 检查草稿归属和原件摘要，检查成员。
2. 读取已确认内容；同内容立即返回，不同内容拒绝。
3. 准备片段与 Embedding。任何外部失败发生时，尚未打开业务写事务。
4. 进入数据库事务并取得同一上传 ID 的事务锁。
5. 再次读取已确认内容，处理准备期间其他请求已经提交的结果。
6. 同一事务内写 reports、metrics、report_chunks。成功退出提交，异常退出回滚。

重复上传同样文件目前会生成两个不同草稿，不具备“文件内容去重”。
并发请求可能都完成外部 Embedding 后才在写事务中串行；业务去重不等于外部请求恰好一次。
确认后修改同一报告内容仍返回 409；本轮没有新增修订版本系统或 worker。

## 4. 固定基线与计数口径

`capture --phase before` 和 `capture --phase after` 使用同一份探针、固定合成文本与受控模型输出。
每轮创建随机临时 schema，加载原 schema 与 M3 迁移，运行流程，最后删除该 schema。
业务成员与报告不写进正式 schema；实际模型配置不被改写。

13 个流程检查分别为：成员列表、上传草稿、草稿不可查询、首次确认、同内容重放、冲突确认、
错成员草稿、错成员指标、已确认指标、已确认草稿详情、普通问答、工具指标问答、越范围问答。

| 计数 | 定义 |
| --- | --- |
| unittest 数量 | unittest 输出的 Ran N tests；不按 assert 个数计数 |
| 流程检查数量 | behavior.json 中 checks 数组的长度；不是额外 13 个模型评测题 |
| 受控调用 | controlled_calls；只是探针替身被执行次数，不是真实 Token 或账单 |
| 外部模型调用 | 此探针固定为 0；没有发生真实模型请求 |
| 数据库结果计数 | 此次临时 schema 中 reports、metrics、report_chunks、query_logs 的行数 |
| 主可靠性场景 | 下一节单独列出的业务场景；不得与测试方法数或流程检查数相加 |

对照去除 query_id、latency_ms、created_at，并把本轮上传 UUID 映射为固定标记；其余响应、
OpenAPI、数据库计数与受控调用次数必须相同。原始片段来源和确认逻辑仍在真实 PostgreSQL 中执行。
本轮不发布准确率、性能提升或生产可用性。统一埋点后的性能基线 V0 在 M9 建立。

## 5. 主可靠性用例预登记

这是后续验收规格。M8 的替身检查不构成真实事务、并发或恢复验证。

| ID | 输入/故障 | 预期状态与业务计数 | M8 已有证据/执行阶段 |
| --- | --- | --- | --- |
| C01 | 同上传 ID、相同确认内容重放 | already_confirmed；报告数和每类指标/片段数不增加；重放不再调用 Embedding | 受控 PostgreSQL 基线已覆盖；M10 纳入系统验收 |
| C02 | 同上传 ID、不同确认内容 | 409；保留原确认值，计数不增加 | 受控 PostgreSQL 基线已覆盖；M10 纳入系统验收 |
| C03 | 同成员、相同原件重复上传；再用相同幂等标识重放 | 当前两个独立草稿是已知现状；M10 明确新增幂等标识的复用和冲突规则，不能把当前行为写成内容去重 | M10 待实现/执行 |
| C04 | 两个独立连接同时确认相同 ID | 一个业务结果，另一个复用；总报告/指标/有效片段只有一份；内容不同则一方明确冲突 | 已核对锁和二次检查，真实并发待 M10 |
| C05 | 插入 reports 后、metrics 中途、chunks 中途分别抛异常 | 写事务全部回滚；旧状态保持；无可见半成品；草稿可重试 | M10 待故障注入 |
| C06 | 已确认报告修订，索引重建失败后重试 | 修订记录可追溯；有效版本规则明确；失败版本不被查询；新版本就绪后切换 | M10 待实现/执行 |
| C07 | 其他成员引用此报告；后续查询试图放宽范围 | 拒绝或空结果；任何证据都不跨出所选成员与报告 | 请求/SQL 范围已有，受控 PostgreSQL 检查已覆盖；M10 扩展 |
| C08 | worker 在外部调用前、结果落盘后、提交前分别中断 | 重启后恢复或明确失败；业务记录不重复；记录可能重发的外部请求 | worker 未实现；M11 待实现/执行 |
| C09 | 旧执行晚于新版本返回 | 执行标识/版本校验拦截旧提交；新版本不被覆盖 | M11 待实现/执行 |
| C10 | 临时接口失败及持续失败 | 临时失败在总预算内有限重试；持续失败在次数/耗时/调用预算内终止 | 现模型客户端不自动重试；任务重试待 M11 |

后续各阶段记录：场景通过数/执行数、业务重复记录数、版本错误数、恢复耗时。
未执行、未实现、已有静态保护和已实测通过分别标注，不互相替代。

## 6. 完成 M8 后能解释的内容

- `/api/metrics` 怎样经过 Router、Service、Repository；SQL 的成员过滤在哪里。
- 上传草稿为什么还不能被问答查到；原件与人工核对值各保存在哪里。
- 确认为什么在模型调用前后各检查一次，以及事务锁保护什么。
- 如何用同一套固定输入验证重构前后行为相同。
- M8 与 M9/M10/M11 的责任范围，以及现在仍未实现的功能。

参考：[FastAPI 依赖注入](https://fastapi.tiangolo.com/tutorial/dependencies/)、
[FastAPI 多文件应用](https://fastapi.tiangolo.com/tutorial/bigger-applications/)、
[psycopg 事务](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。

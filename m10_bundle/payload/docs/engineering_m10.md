# M10：报告数据一致性、质量与来源

本阶段沿用 PostgreSQL / pgvector。向量与关键词索引均在 PostgreSQL 的 report_chunks 表内，可以和正文、指标在同一事务提交。M9 的 V0 保留原件；M10 改动有意增加接口与版本字段。

## 1. 需要解决的问题

同一报告重复提交时，不能重复生成业务记录。两人同时确认或修改时，后到的旧内容不能覆盖已保存版本。确认或修订失败时，读取者继续看到完整的已保存版本。指标和引用需要能回到当时的确认记录及原始抽取。

## 2. 操作约定

| 操作 | 约定 |
| --- | --- |
| 同成员、相同原始文件字节再次上传 | 按 SHA-256 复用上传 ID；文件名不参与内容身份 |
| 相同文件上传到另一成员 | 独立记录，继续受成员过滤约束 |
| 相同文件仍在抽取中 | 409 upload_in_progress；完成后重放可复用 |
| Idempotency-Key 重放相同操作内容 | 返回已提交回执，标记 replayed；上传读取该 ID 的当前状态 |
| 相同成员、操作、Key 用于不同内容 | 409 idempotency_conflict |
| 重复确认相同内容 | 返回 already_confirmed，不再写入业务记录 |
| 用确认接口更改已确认内容 | 409 already_confirmed_changed；通过修订入口提交 |
| 修订 | 携带 expected_version；不匹配返回 409 version_conflict |
| 同内容的修订 | 默认不新增版本；rebuild_index=true 可显式重建并新增处理版本 |
| 事务中写入失败 | 整体回滚，接口返回数据库错误，原保存版本保留 |

Key 按成员与操作隔离，长度 1–128，使用字母、数字、点、下划线、冒号或连字符。修订请求指纹包含报告 ID、内容、预期版本、原因、actor 与重建选项。早先成功请求的同 Key 重放会返回**当时的回执版本**；读取当前版本使用详情或 provenance。

报告 ID 是报告身份，data_version 是内容/处理版本，chunk_version 是切分策略版本，index_version 表示索引对应的数据版本。两个切分策略不是两次报告修订。

## 3. 数据与来源

原有五张业务表保留。新增治理表：

| 表/字段 | 保存内容 |
| --- | --- |
| source_uploads | 成员内文件哈希、原件位置、首次抽取副本；唯一约束 (member_id, file_sha256) |
| report_revisions | 每个版本的确认快照、前后快照哈希、时间、原因与操作标记 |
| revision_sources | 当时的 metric/chunk/report 引用及内容，用于解析历史引用 |
| idempotency_records | 请求指纹及同事务保存的成功回执 |
| engineering_migrations | 数据库迁移版本、源码校验和、执行时间 |
| reports.data_version / index_version / index_state | 当前可见版本与索引状态 |
| metrics / report_chunks.data_version | 当前投影对应的报告版本 |
| query_logs.stale_report_ids | 记录哪些已用来源后来发生了修订 |

报告与成员、指标与报告的外键继续保留；指标和片段增加 (report_id, data_version) 到修订记录的外键。ready 索引必须匹配 data_version。每份报告内指标编码、切分策略与片段序号的唯一约束继续生效。

应用写入采用追加审计记录、替换当前投影的方式。历史快照与引用内容保留；历史版本的向量不长期保留。旧引用可以查看当时来源，但不进入当前检索。

迁移将已有确认值保存成 legacy_snapshot。对于仍有完整草稿及原件的旧上传，读取并校验原件后登记实际抽取副本。缺失或重复的旧原件不使用确认值倒填，来源接口会显示 original_extraction_available=false。合成报告本身没有上传原件。

## 4. 写入边界

上传时先获取按文件/Key 隔离的会话锁，随后进行抽取与完整文件写入，最后发布数据库元信息。外部调用期间没有数据库写事务，但会占用一个数据库连接。本轮适用于现有单机部署。

确认与修订先校验成员、原件、质量和预期版本，然后在事务外准备完整的两套索引。进入写事务后，重新取得报告/Key 的事务锁并检查幂等与版本，再一次提交正文、当前指标、当前片段、修订、来源及回执。新版本准备失败时，已保存版本保持可用。

同一数据库事务保证读取者不会看到只更新了正文或指标的中间状态。PostgreSQL 序列分配可能在回滚后留下空号；验收检查重复业务行与版本关系，不要求 metric ID 连续。

应用不覆盖既有原件。读取原件与确认前后校验文件哈希。文件写完但元信息尚未提交的中断可能留下未登记目录，本轮保留原件，避免在提交结果不明时删除已引用文件。M11 再处理任务租约、恢复和中断窗口。

并发确认可能各自在事务外调用一次 Embedding；保证的是业务写入幂等，不承诺外部模型请求只发生一次。

## 5. 读取一致性

指标查询按成员、已确认状态和当前 data_version 过滤。向量与关键词通道在同一个只读 REPEATABLE READ 快照内核验并检索同一版本。问答 Service 在执行前后比较范围内报告版本；执行期间发生修订则返回 version_conflict，客户端可重新提问。

修订提交时标记使用该报告的旧问答日志。旧日志供历史追踪使用，不作为当前答案缓存复用。读取接口的版本校验是本次响应的检查点；检查点之后发生的修订属于后续状态。

成员维度是请求范围约束。本轮保留本地演示的身份边界，actor 是操作标记，不能当成账号认证或多租户权限系统。

## 6. 质量规则

Review 继续检查必填、ISO 日期、明确有限小数、重复指标编码，以及来源原句、值、单位是否确实出现在确认文本中。M10 补充常用指标的名称/编码映射和显式单位集合。

支持不同合法单位原样保存，不做隐式换算；自定义指标产生人工复核提示。参考范围用于保留报告信息，不用于自动删除范围外的数值。确认不通过时，原件和首次抽取仍保留在草稿中。

数据质量报告还核对连续版本、哈希链、当前投影与快照、片段位置与原文哈希、向量维度、历史来源登记、文件完整性。报告输出规则与计数，避免把真实报告正文写进验收材料。

## 7. 接口

- POST /api/ingest/upload：保留原请求格式，支持可选 Idempotency-Key。
- POST /api/ingest/{id}/confirm?member_id=...：保留 Review 请求，支持可选 Idempotency-Key，增加版本回执。
- POST /api/reports/{id}/revisions?member_id=...：{expected_version, reason, actor?, rebuild_index?, review}。
- GET /api/reports/{id}/provenance?member_id=...：原始抽取、当前版本、各次确认快照与指标来源。
- GET /api/sources/resolve?member_id=...&source_id=...：解析一个历史或当前引用，返回 is_current。

新增失败沿用 M9 的 code / message / request_id / details 契约。接口中传入错误成员时，来源、原件及修订均不会放宽范围。

## 8. 迁移和旧脚本

先停止 app 并备份数据库与 uploads，再在临时 schema 中复制当前五张业务表，演练迁移和再次执行，核对原字段行数及哈希。成功后才升级共享业务 schema。迁移、回填、约束和迁移记录在一个事务提交；失败会回滚。

M10 已登记的数据库停止使用原合成导入与 M3 写索引脚本，防止绕过新审计链。重建使用修订接口的 rebuild_index=true，或在独立测试数据库运行旧数据准备流程。M9 V0 保留原源码与探针，后续对照在对应的数据副本和版本中执行。

升级完成后，单独回退 Python 文件不足以恢复旧系统。升级前备份与原代码共同构成恢复点；升级后新数据需要另行保留。当前提供受保护的升级前代码回滚，数据库已升级时该命令会停止。

## 9. 验证与边界

离线回归使用文件 fixture、受控依赖和内存仓库，检查编排与契约。PostgreSQL 验证创建唯一的临时 schema，使用真实外键、锁、事务和触发器，分别检查重复上传、并发确认、写入故障、修订、来源、质量与原问答流程；退出后清理 schema。不会向共享业务表注入故障。

迁移演练会复制本机现有业务数据到临时 schema，运行结果只保存计数、哈希与状态。故障验证使用合成图片和受控模型。场景数、测试函数数、真实模型质量与生产并发是不同口径。

M11 的 worker 租约、重试与中断恢复，M12 的性能/费用实验，仍按大纲继续。当前不生成性能提升比例或真实模型准确率。

## 10. 阅读顺序

先看 services/governance.py 的 _commit，再看 repositories/governance.py 的 write / save_version；对照 migrate_m10.py 看约束。随后读 data_quality.py、data_audit.py、retrieval.py 和 services/queries.py。最后读 m10_bundle/tests/integration_probe.py 中的并发屏障与数据库触发器。

能解释：为什么锁内还要复查；为什么重复请求的外部调用数与业务行数不同；为什么修订失败仍能查旧版本；为什么旧回执不是当前状态；为什么参考范围外的数值不能被当作脏数据删掉。

官方参考：[PostgreSQL 16 事务](https://www.postgresql.org/docs/16/tutorial-transactions.html)、[显式锁](https://www.postgresql.org/docs/16/explicit-locking.html)、[约束](https://www.postgresql.org/docs/16/ddl-constraints.html)、[事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)。

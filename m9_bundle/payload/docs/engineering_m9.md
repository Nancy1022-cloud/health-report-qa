# M9：接口契约、统一异常与结构化请求日志

适用：health-report-qa，接在 PostgreSQL / pgvector 版本的 M8 后。版本：m9.v1，2026-10-31。

## 1. 本轮行为

核心 JSON API 使用独立输入和输出模型。HTTP 失败返回统一错误对象；正常响应保留原业务字段，并为查询与记录列表增加 `result_code`。所有 HTTP 响应提供服务端生成的 `X-Request-ID`。

请求不符合输入契约是 `422 validation_error`。服务返回值不符合输出契约是 `500 response_contract_violation`；输出错误不能归咎于用户参数。

## 2. 输入与输出

| 入口 | 输入 | 输出契约 |
| --- | --- | --- |
| POST /api/query | QueryRequest：文本长度、严格整数 top_k、检索模式、切分版本、成员与报告范围 | QueryResponse |
| POST /api/agent/query | AgentRequest：问题、至多 5 名成员、报告范围；指定报告必须指定成员 | AgentResponse |
| GET /api/members | 无 | MemberList |
| GET /api/reports | member_id | ReportList |
| GET /api/reports/{report_id} | report_id、member_id | ReportDetail |
| GET /api/metrics | MetricFilters：成员、可选指标、可选日期闭区间 | MetricList |
| GET /api/index-stats | 无 | IndexStats |
| GET /api/query/{query_id}/trace | query_id | TraceResponse |
| GET /api/ingest/members | 无 | IngestMember 列表 |
| POST /api/ingest/upload | multipart：成员、报告文件 | DraftResponse |
| GET /api/ingest/{report_id} | 上传 ID、成员 | DraftResponse |
| POST /api/ingest/{report_id}/confirm | Review：人工勾选、有效日期、指标与原文来源一致 | ConfirmationResponse |
| GET /api/ingest/{report_id}/file | 上传 ID、成员 | PDF / PNG / JPEG 二进制，不套 JSON 模型 |
| GET /health | 无 | HealthResponse |
| POST /api/dev/test-llm | 无 | LLMTestResponse |
| POST /api/dev/test-embedding | 无 | EmbeddingTestResponse |

`date_from`、`date_to` 都可以省略；同时提供时必须前者不晚于后者。SQL 参数化传入日期，且保留成员过滤、报告 confirmed 和指标 confirmed 条件。开始日与结束日均包含在结果内。

正常查询示例：

```text
GET /api/metrics?member_id=member_mom&metric_code=fasting_glucose&date_from=2026-10-01&date_to=2026-10-31
```

顶层响应模型禁止意外字段；Evidence 的共同必填字段为 source_id / report_id / member_id，各工具的扩展字段保留；模型 usage 与已有业务 trace 按 JSON 保留。输出检查还覆盖列表计数、成员一致性、metric 来源 ID，以及问答状态、证据集合与日志链接的一致性。这些结构检查不能证明自然语言回答在语义上正确。

## 3. 系统失败

错误对象示例（request_id 仅为格式示意）：

```json
{
  "code": "validation_error",
  "message": "请求参数不符合接口约定。",
  "request_id": "00000000000000000000000000000000",
  "details": [{"field": "body.top_k", "type": "greater_than_equal"}]
}
```

| HTTP | 机器码示例 | 含义 |
| --- | --- | --- |
| 400 | bad_request | 请求格式问题 |
| 404 | member_not_found / report_not_found / draft_not_found / trace_not_found / resource_not_found | 资源不存在或不在本次成员范围 |
| 405 | method_not_allowed | 请求方法错误；保留 Allow 头 |
| 409 | already_confirmed_changed / source_changed / state_conflict | 已确认内容冲突、原件改变、索引或业务状态不允许 |
| 413 | file_too_large / image_too_large / payload_too_large | 超出内容限制 |
| 415 | unsupported_file / invalid_image | 不支持或损坏的文件 |
| 422 | validation_error / empty_file / invalid_pdf 等 | 请求校验或文件输入不符合约定 |
| 500 | response_contract_violation / internal_error | 输出契约违规或未处理的内部异常 |
| 502 | model_http_error / extraction_http_error / embedding_http_error / upstream_error 等 | 外部模型失败或输出无效 |
| 503 | database_error / storage_error / log_write_failed / dependency_unavailable 等 | 数据库、文件、业务日志或配置不可用 |
| 504 | model_timeout / total_timeout / index_budget_exceeded / upstream_timeout 等 | 单次模型或总时间预算耗尽 |

已有处理流程的细分错误码经过白名单保留；旧的 `request_404` / `request_409` 等包装按 HTTP 类别归一。统一处理器不透传任意异常文本、请求 input 或供应商原始响应。全部稳定保留的旧码见 `app/api_errors.py:KNOWN_CODES`，HTTP 文档中的失败响应统一引用 ErrorResponse。

## 4. 正常业务结果

HTTP 200 不等于“一定得到答案”。调用方应读 `status` 与 `result_code`。

| 业务入口或状态 | result_code | 含义 |
| --- | --- | --- |
| answered | ok | 已生成有引用的回答 |
| retrieved | retrieval_only | 仅检索，未请求生成回答 |
| clarify | clarification_required | 需要用户明确成员或条件 |
| partial | partial_answer | 只回答得到支持的部分；查看正文和工具轨迹 |
| insufficient，范围内没有已确认报告 | no_records | 没有报告记录 |
| insufficient，有范围但检索或证据仍不足 | insufficient_evidence | 不能据现有证据回答 |
| 报告/指标列表为空 | no_records | 该过滤条件下没有记录 |

外部失败不能转换成“没有记录”。原 M5 允许在部分工具失败且已有可引用事实时返回 partial，并在正文说明缺口；整个请求无法完成时使用相应非 2xx 错误。上述约定保留已有业务边界。

## 5. 日志与追踪

`app/observability.py` 使用纯 ASGI 中间件。每次请求创建 32 位十六进制 ID，忽略客户端提供的同名头。请求 ID、HTTP 响应头、错误 body 和本次请求的阶段日志相互对应。

新增请求日志写到标准错误输出，同时默认保存在：

```text
app/data/m9/runtime/requests.jsonl
```

每个日志文件最大约 5 MB，保留 3 个轮转备份；当前使用单进程应用。后续增加多个进程时，应把标准输出交给日志收集器，不能依赖这个单进程轮转文件设计。

日志事件：

- `stage.completed`：阶段名、耗时、ok/error。
- `contract.violation`：服务端响应契约出错的字段位置和错误类型，不含返回值。
- `request.completed`：路由模板、方法、HTTP 状态、耗时、失败码/异常类、业务状态、query_id 与阶段列表。

不记录请求正文、原报告、问题文本、查询字符串、原始文件名、Key 或异常文本。未知路径记作 `<unmatched>`，报告路径记作 `/api/ingest/{report_id}` 这样的模板。已有数据库 query_logs 仍保留原有业务问答轨迹；新增请求日志和该表的用途不同。

已建立的阶段包括 records.*、query.pipeline、agent.pipeline、report.create_draft、report.extract、report.draft_store、report.confirm、report.index_prepare、report.confirm_transaction。外层阶段包含内层阶段，不能直接求和；模型、检索、锁等待等更细粒度埋点在 M12 按实际需要补充。

单个 request_id 的 request.completed 只有一条。未处理异常会继续交给 ASGI 最外层发送统一 500，完成日志的 response_body_complete 此时为 false，表示该日志层未观察到最终错误 body，不能当作已确认送达客户端。

request_id 与 query_id 用途不同：一个关联 HTTP 请求；另一个关联原有问答数据库轨迹。问答成功或带 query_id 的失败会将两者记录在同一条请求日志中。

## 6. V0 基线

`m9_bundle/run_m9.py baseline` 在统一埋点后的版本保存 V0。默认 3 轮，每轮 26 个场景，包括 M8 的 13 个核心流程和本轮新增的参数、日期、空记录、日志、索引统计与原件读取检查，共 78 次场景执行。

每轮使用新 PostgreSQL 临时 schema 和临时上传目录，退出时清理。使用当前配置的向量维度和固定向量规则，替换外部抽取、向量化、生成与工具决策；固定输出和无额外人工延时的配置保存为 fixture。业务数据库中原有五表的行数在采集前后只读核对；采集时请暂停其他手动操作。

保存：

- 每次请求的预期/实际状态、业务结果、request_id、阶段和耗时。
- 逐次请求日志和每轮 fixture，固定任务清单。
- Python / 依赖、真实数据库 / 索引、当前模型名称与端点主机等配置；不含 Key 或口令。
- 当前源码快照与哈希、受控探针及其哈希。
- 按场景分组的中位数、最小值、最大值和样本量。

计数口径是“场景是否符合预期”，包括应返回 4xx 的负例；不得把 78/78 描述为真实模型问答准确率。3 轮属于初始受控观测，不估算 P95，也不代表并发吞吐或生产性能。

client_duration_ms 包含 TestClient 的同进程 HTTP 调度；server_duration_ms 记录中间件经过的处理时间。它们不含真实公网传输、真实模型延时或异步队列等待。usage 里的替身数字不能作为实际 Token；`real_model_usage` 为 null。

已成功冻结的 V0 禁止覆盖。失败尝试保留后再运行。M10/M11 之后可对相同任务与受控配置重跑，共同口径优先比较正确性与数据计数；如果引入异步，要将 API 接收耗时与“提交到任务终态”的端到端耗时分别报告，不能把立即返回当成加速完成。

## 7. 阅读顺序与后续

1. schemas/queries.py、schemas/filters.py、schemas/responses.py：输入和输出契约。
2. query_routes.py / record_routes.py / upload_routes.py：HTTP 如何声明契约。
3. api_errors.py：校验错误、业务错误与系统异常的映射。
4. observability.py 与 services/reports.py：请求 ID、阶段与失败的关联。
5. m9_check.py：正常、负例、故障替身和日志断言。
6. V0/task_manifest.json、observations.json 与 timing_by_scenario.json：统计口径。

M10 扩展事务、幂等、修订和来源追溯；M11 处理持久任务恢复；M12 继续测量并选择一个实际瓶颈优化。

## 官方资料

- https://fastapi.tiangolo.com/tutorial/response-model/
- https://fastapi.tiangolo.com/tutorial/handling-errors/
- https://docs.pydantic.dev/latest/concepts/validators/
- https://www.starlette.io/middleware/

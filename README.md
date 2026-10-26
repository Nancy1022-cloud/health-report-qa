# 健康报告智能检索问答

从零开发的家庭健康资料查询原型。上传时选择成员，抽取后人工核对日期、数值与单位，再查询已确认指标和报告原文，回答保留来源。第一版用于资料整理与查询。

## 实现范围

- FastAPI、单文件 HTML、PostgreSQL 16 与 pgvector，Docker Compose 两个服务。
- 固定切分与章节切分并存；中文分词后做 PostgreSQL 全文检索，与向量排名通过 RRF 融合。
- 三个真实工具：`get_report_list`、`get_metric_trend`、`search_reports`。成员和报告范围由后端校验。
- 模型选择工具并生成参数；查询与同单位差值由程序执行。单位不同不直接相减。
- 工具循环最多执行 6 次工具、10 次模型请求；补检索最多一次、回答修订最多一次，总流程等待预算为 120 秒，日志写入另有 10 秒等待预算。
- 对单成员、单报告、单指标且符合条件的纯数值问题可直接按结构化记录组织回答；其他问题按证据需求检查。
- PDF 文字提取、图片识别、草稿核对和确认入库；未确认草稿不进入问答范围。
- `query_logs` 保存工具参数、结果、检索过程、纠错事件和接口返回用量。

全文检索使用 PostgreSQL 排名函数，不称为 BM25；应用层证据检查与有限纠错不宣称复现论文版 Self-RAG。成员过滤用于单家庭演示中的数据归属约束。

## 运行

环境：Docker Desktop、Docker Compose，能够访问所配置模型服务的网络与 API Key。

在项目根目录复制配置，并填写本机已经验证的服务地址、模型名和 Key：

```powershell
Copy-Item .env.example .env
```

配置生成、视觉与 Embedding 三组变量。向量维度须与模型返回结果一致；更换向量模型或维度后，应重新构建相应索引。

全新数据库首次运行，依次执行：

```powershell
docker compose up -d --build
docker compose exec app python -m app.init_data
docker compose exec app python -m app.migrate_m3
docker compose exec app python -m app.m7_data seed
docker compose exec app python -m app.m3_index --chunking fixed
docker compose exec app python -m app.m3_index --chunking section
docker compose exec app python -m app.m7_data snapshot
```

每条命令成功后再执行下一条。`init_data` 和 `m7_data seed` 仅导入经过逐字校验、明确标记为合成的固定测试数据；真实上传使用 `/upload` 核对确认。已有 M0～M6 数据的项目直接按 M7 指导增量操作。

- [健康检查](http://localhost:8000/health)
- [上传与核对](http://localhost:8000/upload)
- [接口说明](http://localhost:8000/docs)

## 接口

| 接口 | 用途 |
|---|---|
| `GET /api/members` | 成员列表 |
| `GET /api/reports?member_id=...` | 某成员已确认报告 |
| `GET /api/metrics?member_id=...` | 某成员已确认指标 |
| `POST /api/query` | 向量或混合检索实验入口 |
| `POST /api/agent/query` | 工具调用和证据纠错问答 |
| `GET /api/query/{query_id}/trace` | 读取本次问答日志 |
| `POST /api/ingest/upload` | 上传文件并生成草稿 |
| `GET /api/ingest/{report_id}?member_id=...` | 读取草稿或确认结果 |
| `POST /api/ingest/{report_id}/confirm?member_id=...` | 核对后确认入库 |

Agent 请求示例：

```json
{
  "question": "验收甲10月15日的空腹血糖是多少？",
  "member_ids": ["m7_member_a"],
  "report_ids": ["m7_a_20261015"],
  "chunk_version": "section200o40v1",
  "retrieval_mode": "hybrid"
}
```

Agent 使用 `member_ids` 列表；基础检索使用 `member_id`。回答状态为 `answered`、`partial`、`insufficient` 或 `clarify`。超时、数据库或模型故障走独立错误响应，不能把 HTTP 5xx 当成正常“没有记录”。

## 数据与文件

五张业务表：`family_members`、`reports`、`report_chunks`、`metrics`、`query_logs`。

| 文件 | 职责 |
|---|---|
| `app/rag.py`、`app/retrieval.py` | 模型调用与检索 |
| `app/chunking.py`、`app/text_search.py`、`app/rrf.py` | 切分、关键词检索与排名融合 |
| `app/agent_tools.py`、`app/agent.py` | 工具协议、校验及循环 |
| `app/evidence_guard.py` | 覆盖检查、一次补检索、引用校验和有限修订 |
| `app/report_extract.py`、`app/report_ingest.py` | 文件抽取、草稿与事务入库 |
| `app/m7_data.py`、`app/m7_eval.py` | 合成验收数据、逐题运行与结果汇总 |

数据库通过 Compose 命名卷保存，原件和草稿通过 `./uploads:/app/uploads` 保存。上传目录和带真实资料的原始日志不进入代码仓库。`docker compose down` 可停止服务并保留命名卷；重新启动使用 `docker compose up -d`。

## 评测

已有 M2/M3 开发题和历史结果保留。M7 新增两个专用测试成员、12 份合成报告、36 条指标、20 道 Agent 验收题，以及 8 道原文检索对照题。新验收题须在冻结实现后首次运行；看过结果并据此改动后，再跑属于回归测试。

离线回归检查：

```powershell
docker compose exec app python -m app.m5_check
docker compose exec app python -m app.m6_check
docker compose exec app python -m app.m7_check
```

真实模型与数据库验收：

```powershell
docker compose exec app python -m app.m7_eval run --label first-acceptance
```

结果保存在 `app/data/m7_runs/<本轮目录>/`。逐题核对 `results.json` 后，在 `manual_review.json` 填写三个字段：答案正确、引用支持对应结论、工具参数正确。再执行：

```powershell
docker compose exec app python -m app.m7_eval report --run-dir /app/app/data/m7_runs/本轮目录名
```

自动项只检查接口状态、来源真实性、范围、工具出现情况、证据覆盖和调用预算。语义正确性与完整参数正确性由人工核对；模型自评不作人工评分。

检索实验沿用 M3：

```powershell
docker compose exec app python -m app.eval_m3 --dataset /app/app/data/eval_m7_retrieval.json --top-k 2
```

依次比较固定切分＋向量、章节切分＋向量、章节切分＋混合检索。使用相同题目、成员范围、模型及 `top_k`。原文字符区间的证据覆盖比例用于比较，不把它视为答案正确率。

实测结果整理到 `docs/m7_acceptance.md`、`docs/m7_retrieval.md`。未运行前不填写通过率或提升比例。当前没有“关闭证据检查”的同条件实验，因此只展示纠错行为与终止预算，不声明其带来的准确率提升。

## 演示与限制

演示顺序见 `docs/m7_demo.md`：精确数值、跨报告组合、缺失资料，以及一份上传报告的核对入库与问答。

第一版未提供账号授权系统、长期会话记忆、单位换算或复杂 OCR 版式支持；只验证当前实现支持的输入。小规模合成集结果用于解释项目行为，不能外推至真实医疗场景。

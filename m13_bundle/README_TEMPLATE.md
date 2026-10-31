# 健康报告智能检索问答

将合成／脱敏健康报告上传并人工核对后，按家庭成员查询指标和报告原文，回答附引用。本项目重点验证报告写入与修订的一致性，以及文档处理任务中断后的恢复。

技术栈：Python 3.12、FastAPI、Pydantic、PostgreSQL 16 + pgvector、OpenAI 兼容接口、Docker Compose。API 与单个独立 worker 共享数据库和上传目录。

## 工程设计

- Router / Schema / Service / Repository / Client 分层；核心业务可通过注入替身独立验证。统一参数、业务冲突、上游失败和内部错误响应，并以 request_id 关联日志。
- 报告正文、指标、索引、修订快照与回执在写事务中保持版本关系。相同内容重放复用，内容冲突明确拒绝；数据库约束和提交前复查处理并发确认。
- 持久化任务保存处理代次、阶段结果、调用预留和终态；租约与执行 token 阻止旧执行提交。外部请求放在写事务之外，有限重试共用任务预算。
- 分阶段计时保留重叠区间；任务完成耗时包含排队与重试。缺失 usage 保留未知。故障窗口可能导致外部请求重发，不声称外部接口 exactly-once。

## 当前证据

以下数字由本机验收记录生成，具体机器、参数和原始记录另存于阶段证据目录。

| 项目 | 本次记录 | 口径 |
| --- | --- | --- |
| 离线自动化测试 | {{local_unit_tests}} | unittest 测试方法数量，含业务、引用、契约、任务与交付门槛 |
| 数据库探针断言 | {{engineering_scenario_checks}} | 多个探针的检查项总数，不能解释为同数量独立业务任务 |
| 受控独立确认请求 | {{independent_requests}} | 并发 1/3/5，单 worker，批量 1/4，独立文件 |
| 重复确认请求 | {{duplicate_requests}} | 幂等验证，与独立处理吞吐分开 |
| 真实 Embedding 索引任务 | {{real_embedding_tasks}} | 仅统计完成的 M12 正式批次；不含 pilot |
| M12 全范围完成 | {{M12_full_scope_completed}} | 真实测量缺口会保留 |

性能对照见 [CSV](docs/results/m12_comparison.csv) 和 [图表](docs/results/m12_latency.svg)。本次部署建议：`{{deployment_recommendation}}`。批量减少请求次数不等于 Token 或费用同比减少，结果不预设提升比例。

CI 工作流位于 `.github/workflows/health-m13.yml`，使用临时 pgvector 服务和受控模型，运行业务／引用回归、契约、一致性与进程故障检查。远端实际结果请查看仓库 Actions；提供工作流不等于远端已运行成功。

## 运行

已有环境使用原来的 `.env`，无需重新导入数据：

```powershell
docker compose -f compose.yaml -f compose.m11.yaml up -d db app worker
```

M12 已选择批量 4 的环境，继续加 `-f compose.m12.yaml`。浏览器打开 `http://127.0.0.1:8000/`、`/upload` 和 `/docs`。启动后等待 `/health` 正常以及 `/api/tasks/worker` 的 `online=true`。

全新环境初始化见 [运行与恢复说明](docs/RUNBOOK_M13.md)；模型名、地域和 Key 以当前账户实际可用配置为准。受控 CI 不需要收费模型 Key。

## 验证和演示

在已完成 M12 的项目根目录执行：

```powershell
. .\m13_bundle\HealthM13.ps1 -BatchSize 1
Invoke-HealthM13 check --database
Invoke-HealthM13 demo
Invoke-HealthM13 recompute
```

新克隆仅运行 CI 检查时，按 [运行说明](docs/RUNBOOK_M13.md) 使用一次性数据库；历史验收与 V0 不可重新创建后冒充原始证据。

三个演示包括正常上传与带引用问答、重复确认与真实进程中断恢复、指标修订与来源追溯。自动演示使用合成数据和受控抽取／生成，实际经过路由、数据库、索引、检索与引用校验。详见 [演示说明](docs/DEMO_M13.md)。

## 范围

当前是本地单主机、单 worker 演示服务；成员范围过滤不是多租户鉴权。未验收生产容量、跨主机共享存储、高可用或生产安全。M12 负载使用进程内测试 worker，不包含生产每任务子进程启动开销，后者另做正确性探针。

M13 的工程测试数不是模型问答准确率，也没有新增真实问答质量百分比。原有问答评测文件和修复记录保留；历史 20 题结果不视为当前源码重新评测结果。真实模型用量仅覆盖明确完成的 Embedding 索引批次。

实际上传文件、备份、Key 和逐次运行记录保留在本机；仓库公开结果仅包括选定汇总与合成演示说明。

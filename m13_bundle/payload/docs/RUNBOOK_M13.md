# 运行、升级、持久化与恢复

## 当前部署

`compose.yaml` 定义 PostgreSQL 与 API，`compose.m11.yaml` 增加独立 worker。`postgres_data` 卷保存数据库，宿主机 `uploads/` 同时挂给 API 和 worker。已有环境沿用模型、维度和原件目录。

默认启动：

```powershell
docker compose -f compose.yaml -f compose.m11.yaml up -d db app worker
```

选择批量 4 的环境继续加 `-f compose.m12.yaml`。前端接口返回 202 表示任务已接收；轮询任务到 succeeded 才表示处理成功。`failed` 与 `superseded` 是不同终态。

## 全新环境

新克隆首先复制 `.env.example` 为 `.env`，填写数据库本地密码、模型 Key 与实际可用模型。不要覆盖已有 `.env`。以下只用于全新的空数据库，不用于把现有阶段重置：

```powershell
docker compose -f compose.yaml -f compose.m11.yaml build app worker
docker compose -f compose.yaml -f compose.m11.yaml up -d db
docker compose -f compose.yaml -f compose.m11.yaml run --rm -T --no-deps app python -c "from app.db import init_db; init_db()"
docker compose -f compose.yaml -f compose.m11.yaml run --rm -T --no-deps app python -m app.init_data
docker compose -f compose.yaml -f compose.m11.yaml run --rm -T --no-deps app python -m app.migrate_m3
docker compose -f compose.yaml -f compose.m11.yaml run --rm -T --no-deps app python -c "from app.db import connect_db; from app.migrate_m10 import migrate as m10; from app.migrate_m11 import migrate as m11; print(m10(connect_db)); print(m11(connect_db))"
docker compose -f compose.yaml -f compose.m11.yaml up -d app worker
```

每条命令成功再执行下一条。初始化导入合成报告，但不自动为这些报告调用收费 Embedding 建索引。新上传并确认的报告由 worker 构建索引；查询预置报告原文前需按原 M3 文档完成索引构建并单独登记真实调用。任务演示和 CI 使用临时 schema 自建数据，不依赖这一步。

## 升级与迁移

M10、M11 在 `engineering_migrations` 保存版本与源码校验和。M12/M13 没有新增数据库迁移。已有迁移文件不得直接修改来掩盖差异；后续数据库变化应增加新的迁移，在副本演练后部署。

保留 M11 当时升级前的 `rehearsal/summary.json`、`migration/summary.json`，以及源码／备份。升级完成后重新造一份“升级前演练”不能替代原件。M13 在恢复副本重复调用 M10/M11，要求 already_applied 且不改变数据；这验证迁移幂等，不声称是新一次历史升级演练。

## 已实现备份与恢复演练

从项目根目录加载脚本，BatchSize 与实际日常配置一致：

```powershell
. .\m13_bundle\HealthM13.ps1 -BatchSize 1
Backup-HealthM13
```

执行过程：确认没有 queued/running 任务，停止 API 和 worker；记录所有 public 表、字段、约束、索引、序列、扩展、行内容指纹及 uploads 文件 SHA256；生成 custom-format 数据库 dump 和 uploads.zip；恢复到随机命名的全新数据库与独立 uploads 目录；比较全部指纹、质量规则和迁移幂等；删除演练数据库，保留本机备份；重启现有服务并再次检查业务记录、任务记录和文件。重启后 worker 心跳变化属于预期，其他表仍需一致。

数据库 dump 在容器内直接写文件，再通过 docker cp 复制，避免旧版 PowerShell 重定向改变二进制。主数据库始终不作为 pg_restore 目标。出错时脚本尝试恢复 app / worker；临时数据库清理失败会显示其名称，不能把失败记录改成通过。

备份位于 `backups/m13_时间_随机串/`，包括 database.dump、uploads.zip、snapshot.json 和校验记录；可能包含真实数据，保留在本机。`app/data/m13/v1/restore/summary.json` 是可分享的布尔／计数验收结果，不包含数据库内容。配置和凭据需单独安全保存；单库 pg_dump 不包含集群角色／表空间，也不是连续时间点恢复。

## 故障定位与回退

先按 request_id 查 API 日志，再按 task_id 看事件、阶段结果、attempts、calls_reserved、error_code 和终态。预算、重试及租约的原理见 engineering_m11.md；批量与计时边界见 engineering_m12.md。

worker 中断、调用前／后窗口和旧执行迟到由 M13 check --database 的隔离探针反复验证。日常真实任务中断时先保留原件、任务与调用记录，等待租约策略恢复；不要手改任务成功或删除未知用量账本。

只需要调整批量时用 Compose 配置切换，保留任务提交时保存的配置。代码回退须先停写并核对活动任务、源码版本与迁移兼容性。M13 只新增验收／文档，apply 的备份位置见 installation.json，原 README 另有一次性备份。灾难恢复时先按本次副本演练方式验证备份，再在新部署切换数据库与文件目录；本包不提供覆盖现有业务库的一键破坏性恢复命令。

## 一次性 CI

GitHub Actions 自动创建 pgvector 服务；本地人工运行 `python m13_bundle/run_m13.py ci` 时必须提供全新空数据库、`M13_CI_DISPOSABLE=1` 和对应连接参数。不要对现有业务库设置这一标记。CI 拒绝在已有 family_members 的数据库上初始化，检查子进程只允许数据库和本地受控 HTTP 连接；不读取账户收费模型 Key。

## 依据与边界

- [GitHub：PostgreSQL 服务容器](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)
- [PostgreSQL 16：pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
- [PostgreSQL 16：pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)
- [Docker：依赖启动与健康检查](https://docs.docker.com/compose/how-tos/startup-order/)

按 2026-10-31 核对文档编写；Compose 启动成功后仍需检查服务就绪。结果适用于本次本机环境，不能据此外推生产可用性。

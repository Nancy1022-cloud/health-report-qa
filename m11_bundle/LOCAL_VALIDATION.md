# 本包验证记录

- 228 项离线检查通过：原有 188 项 + 25 项 M11 策略/阶段/文件/契约检查 + 15 项 M11 安装器检查。
- 真实 PostgreSQL 16.15 / pgvector 0.6.0 共 86 个场景通过，其中 29 个为本轮任务与恢复场景；其余为 M10 一致性和既有业务回归。
- 验证了合成数据副本演练、重复迁移、既有九张业务表哈希不变、临时 schema 清理、独立 API 与 worker 联通、硬时限终止子进程。
- 抽取和索引故障使用独立子进程直接退出，等待租约真实过期；保存后的阶段复用，旧 token / 旧处理代次不能提交。
- 在明确标记的 M10/V0 本地验收 fixture 上完成 preflight / apply / check / backup / rehearse / migrate / live / verify / quality / finalize 全链路。该 fixture 只用于验证安装与门禁，不能冒充用户原始 M10/V0 证据；实际迁移和数据库验证使用真实临时 PostgreSQL。
- 最后生成的是技术门禁通过、人工复核未完成的草稿；你的最终验收需按指南在自己的项目生成。
- Python 编译、页面 JavaScript 语法与 Compose YAML 解析检查通过。API 和 worker 以真实独立进程运行；本环境未执行 Docker 引擎或 Windows PowerShell，本机步骤仍须执行。
- 模型全部为受控替身，未执行真实模型评测、收费调用或性能提升实验。源代码、验收脚本及安装清单的一致性均已核对。

请以你本机 app/data/m11/v1 的实际结果作为项目证据。当前 Docker 配置沿用你现有 pgvector 镜像，本地插件版本不同的验证不能代替本机验收。

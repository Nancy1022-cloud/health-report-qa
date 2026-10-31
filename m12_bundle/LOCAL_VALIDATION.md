# M12 配套包验证范围

- 284 项离线／本地受控接口检查通过：既有 228 项回归 + 本轮 56 项检查。完整日志见 validation/offline_tests.txt。
- 新增检查覆盖向量 index／数量／维度／有限数值、未知 usage、同步标准价与地区、持久预算、区间嵌套、安装原子恢复、旧证据保护、验收范围以及报告复算。
- 受控 HTTP 检查实际运行 OpenAI SDK 到本地接口，故意倒序返回向量，未请求外部模型。
- 用明确标记的合成 prior-evidence fixture 验证安装器和完成门槛；它们仅存在单元测试临时目录中，不是用户 M9 V0、M11 或 M12 验收。
- Python 语法和当前 payload／harness 摘要一致性已核对。
- 本环境没有执行真实 PostgreSQL／pgvector 探针、Docker 引擎或 Windows PowerShell；也没有执行收费接口或性能负载。指南中的本机命令负责生成这些实际证据。
- 包内没有预填性能提升、真实 Token、费用或 passed=true 的用户验收。分析报告由本机原始记录生成；完整 M12 需要本机 full_scope_completed=true。

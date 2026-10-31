# M13 配套包验证范围

- 使用公开项目 fe9a983a 基础源码与原始 M8–M12 payload 组成校验副本，核对 M12 源码摘要相符。
- 317 项离线检查通过：既有 284 项 + M13 新增 33 项。完整日志见 validation/offline_tests.txt。
- 新增检查覆盖：失败技术项／未人工复核／缺少真实测量或 CI 时不能错误标为全范围完成；旧证据变更与过期结果；新备份／服务检查失败使旧成功记录失效；远端失败或源码不同不能用本地 artifact 冒充通过；二进制文件备份恢复、归档越界与覆盖保护；重启检查只允许 worker 心跳变化；子进程外网拦截。
- 最终 Python 语法、工作流 YAML 结构和配套包摘要已核对。外部模型调用为 0。
- CI 远端核对测试使用明确标记的临时合成 artifact 与受控 API 返回，不是用户实际 GitHub CI 证据。
- 本环境未执行 PostgreSQL／pgvector 探针、Docker、Windows PowerShell、真实备份恢复、页面或远端 GitHub Actions；这些必须由指南中的本机步骤生成记录。
- 不附带用户 M13 passed=true 验收，不预填数据库／演示／CI／性能结果，不修改或补造 M9–M12 原始验收。

- 编码修复：HealthM13.ps1 已保存为 UTF-8 with BOM；校验文本与修复前完全相同，规范化摘要不变。已检查压缩包中的 BOM 与内容一致；未在 Windows PowerShell 环境实际执行。

- 恢复探针修复：原 M11 源码哈希未变，新探针每个 record 的判定表达式与原件 AST 一一相同；新增 7 项检查覆盖原 token 续租、停止线程、fencing、续租错误、业务异常、批量隔离及失败记录导出。尚需在用户 PostgreSQL 环境执行 recovery 和完整 check --database。

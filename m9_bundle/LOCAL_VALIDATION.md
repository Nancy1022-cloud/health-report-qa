# 本次交付验证记录

2026-10-31。参考源码：GitHub fe9a983a40398a16895006a4438fdcb0b2aebf4e，加已交付 M8 分层 payload。

## 已在本次环境执行

- 原 M5/M6/M7/M8 的 86 项离线回归通过。
- 加入 50 项 M9 契约、异常、日志及应用主入口检查后，应用离线检查 136 项通过。
- 安装器的 14 项检查通过：重复执行、未知差异拦截、Windows 换行/BOM、原字节回退、后续编辑保护、安装失败还原、路径约束与冻结记录保护。
- 在参考 M8 副本执行完整 preflight → apply → check：150 项通过；在副本验证 rollback 恢复，再安装与检查通过。
- 运行实际 FastAPI/uvicorn HTTP 栈并执行 live 检查：health、OpenAPI、首页、上传页、docs、统一 422 和 request_id 日志关联通过。该项数据库健康函数使用受控替身，首页使用本地 QA fixture，不作为用户数据库或浏览器人工验收。
- 上传页面 JavaScript 语法检查通过。
- 全部 Python 文件语法编译通过；固定 PostgreSQL 探针静态核对为 26 个不同场景。

## 待用户环境执行

本次环境未提供 Docker / PostgreSQL / pgvector 服务。真实 PostgreSQL 的临时 schema 流程、日期 SQL 实际执行、业务表计数不变与清理，必须在用户 Docker 环境执行 baseline 后判断。本包不附带伪造的 V0 或用户验收结果。

用户环境还需执行 check、live、baseline，并完成页面错误提示与阅读核对。实际结果写入 app/data/m9/v1，finalize 根据这些记录生成验收表。

本轮受控检查不调用真实模型，不评价生成质量、真实 Token、生产负载或性能提升。

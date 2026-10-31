# 本包本地验证

- 188 项离线回归通过，包括原 150 项、24 项 M10 业务检查、14 项 M10 安装器检查。
- 在已知 M9 源码和明确标记的验收/V0 测试 fixture 上完成安装、check 和字节回滚核对。测试 fixture 不代表用户的实际 M9 数据库。
- Python、页面 JavaScript 语法检查通过；静态迁移/SQL 语法解析通过。
- 未在本环境执行真实 PostgreSQL 的迁移、并发与故障测试。请按指南在本机 Docker 中运行 rehearse / migrate / verify / quality 后取得实际结果。
- 自动检查使用受控模型；未测量真实模型效果、Token 或性能提升。

本机验收结果保存在 app/data/m10/v1，原 M9 V0 继续保留。

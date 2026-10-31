# 三个可重复的演示

先在项目根目录加载 `m13_bundle/HealthM13.ps1`，运行 `Invoke-HealthM13 check --database` 和 `Invoke-HealthM13 demo`。两条命令都不调用收费模型；探针使用临时 schema 与合成报告。每次新运行保留独立目录。

## 1. 正常上传与带引用问答

打开 `app/data/m13/v1/demos/summary.json` 中 output 对应目录。`normal_and_revision/synthetic_report.png` 是合成原件。查看 upload.json、question_before.json：multipart 上传返回任务 ID，抽取后人工核对格式提交确认任务，直到 succeeded；问答返回 5.7 mmol/L 及真实索引片段引用。

抽取／生成模型使用受控替身，生成结果仍经过应用原有引用校验。实际路由、PostgreSQL、索引、关键词检索、问答日志均执行。该演示不衡量视觉识别或模型回答准确率。

讲解顺序：原件 → task_id → 草稿字段 → 确认任务终态 → 引用 source_id。指出 202 和处理完成的区别。

## 2. 重复确认与进程中断恢复

正常演示中的 duplicate_confirmation_reuses_task 检查同一提交复用任务。独立进程故障证据由 summary.json 的 process_recovery_result 指向本次 check 的 m12_batch_recovery 目录。

重点查看 checks.json：persisted_batch_reused_after_process_crash、unsaved_batch_may_resend_and_is_unknown、batch_idempotent_replay；再看 crash_stage_saved.txt、crash_external_returned.txt 与 raw_database_records.json。

讲解：阶段结果已持久化时可以复用；外部接口返回但未持久化的窗口可能重发，原调用标为 unknown；业务最终记录保持唯一。check 的 m11_recovery 另外覆盖执行 token、租约与预算结束，不手改任务记录模拟重启。

## 3. 修订与来源追溯

查看 question_after.json、provenance.json、old_source.json：将合成值 5.7 修订为 5.8 后，报告与索引版本均为 2，新查询只引用新片段；旧 source_id 可解释为过期来源，原始抽取与两版修订快照继续保存。

讲解：原始抽取和人工确认值分别保留；修订影响索引与引用；成员 b 读取成员 a 任务返回 404。

## 页面人工演示

在服务运行时打开 `/upload` 和首页，核对任务状态、错误提示、引用与追溯入口仍能展示。可以沿用 M12 之前已经保存的合成报告与问答；不需要再消耗真实额度来满足 M13 的受控验收。要演示新的真实上传／问答，单独记录真实接口与结果，避免混入受控测试口径。

建议演示总长 3–5 分钟。自动记录用于复核，面试时也要能解释对应代码和状态变化。

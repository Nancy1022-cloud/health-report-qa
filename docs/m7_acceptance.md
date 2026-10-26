# M7 实测验收结果

运行：20261024T033152_423743Z；标签：first-acceptance

| 项目 | 实测 |
|---|---|
| expected_cases | 20 |
| processed | 20 |
| automatic_pass | 17/20 |
| manual_completed | 20/20 |
| manual_all_three_pass | 17/20 |
| unexpected_http_or_transport | 0 |
| required_evidence_fully_covered | 14/14 |
| mean_elapsed_ms_all_processed | 12507 |
| p95_elapsed_ms_all_processed | 26316 |
| observed_total_tokens | 202873 |
| requests_with_incomplete_usage | 0 |
| valid_frozen_run | True |

自动通过只表示结构、来源、范围、预算及标注证据检查通过；语义结论以人工核对为准。

| 题号 | HTTP | 状态 | 自动 | 人工三项 | 失败检查 |
|---|---|---|---|---|---|
| m7_01 | 200 | answered | True | True |  |
| m7_02 | 200 | answered | True | True |  |
| m7_03 | 200 | insufficient | False | False | status_expected |
| m7_04 | 200 | answered | True | True |  |
| m7_05 | 200 | answered | True | True |  |
| m7_06 | 200 | answered | True | True |  |
| m7_07 | 200 | answered | True | True |  |
| m7_08 | 200 | answered | True | True |  |
| m7_09 | 200 | answered | True | True |  |
| m7_10 | 200 | answered | True | True |  |
| m7_11 | 200 | answered | True | False |  |
| m7_12 | 200 | answered | True | True |  |
| m7_13 | 200 | insufficient | True | True |  |
| m7_14 | 200 | answered | False | True | status_expected |
| m7_15 | 200 | partial | True | True |  |
| m7_16 | 200 | clarify | True | True |  |
| m7_17 | 200 | insufficient | False | False | status_expected |
| m7_18 | 404 | 错误/拒绝 | True | True |  |
| m7_19 | 404 | 错误/拒绝 | True | True |  |
| m7_20 | 200 | answered | True | True |  |

耗时包含失败请求；Token为接口已返回用量，缺失部分不是0。
任何改动后的补测均为另一轮回归，保留首轮文件；不以新结果替换旧结果。

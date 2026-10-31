"""Generate a bounded report from actual experiment records, without presupposing a speedup."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from m12_bundle.analysis import write, summarize_groups, comparison, chart, csv_file, usage_summary
from m12_bundle.cost import costs, beijing_endpoint


def number(value, digits=2):
    return f'{value:.{digits}f}' if isinstance(value, (int, float)) else '未知／未执行'


def operational(root, evidence, source):
    from app.db import connect_db
    from app.data_audit import audit
    from app.repositories.drafts import DraftRepository
    from psycopg.rows import dict_row
    from urllib.parse import urlsplit
    with connect_db() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.row_factory = dict_row
        tasks = conn.execute('SELECT id,kind,status,attempts,calls_reserved,error_code,created_at,finished_at FROM processing_tasks ORDER BY created_at').fetchall()
        calls = conn.execute("SELECT c.id,c.task_id,c.prefix,c.status,c.attempt,c.usage,c.error_code,c.created_at,c.finished_at,"
                             "t.payload->'config'->>(c.prefix||'_MODEL') AS model,"
                             "t.payload->'config'->>(c.prefix||'_BASE_URL') AS endpoint "
                             'FROM processing_calls c JOIN processing_tasks t ON t.id=c.task_id ORDER BY c.created_at').fetchall()
        timed = conn.execute("SELECT task_id,attempt,detail FROM processing_events WHERE event='attempt_timing' ORDER BY id").fetchall()
        rules = {
            'active_generation_mismatch': "SELECT count(*) AS n FROM processing_tasks t LEFT JOIN processing_heads h ON h.member_id=t.member_id AND h.report_id=t.report_id WHERE t.status IN ('queued','running') AND (h.task_id IS DISTINCT FROM t.id OR h.generation IS DISTINCT FROM t.generation)",
            'artifact_fingerprint_mismatch': 'SELECT count(*) AS n FROM processing_artifacts a JOIN processing_tasks t ON t.id=a.task_id WHERE a.fingerprint<>t.fingerprint',
            'call_reservation_count_mismatch': 'SELECT count(*) AS n FROM processing_tasks t WHERE t.calls_reserved<>(SELECT count(*) FROM processing_calls c WHERE c.task_id=t.id)',
            'terminal_call_left_reserved': "SELECT count(*) AS n FROM processing_calls c JOIN processing_tasks t ON t.id=c.task_id WHERE t.status IN ('failed','succeeded','superseded') AND c.status='reserved'",
            'succeeded_without_result': "SELECT count(*) AS n FROM processing_tasks WHERE status='succeeded' AND result IS NULL"}
        task_rules = {name: conn.execute(sql).fetchone()['n'] for name, sql in rules.items()}
    quality = audit(connect_db, DraftRepository())
    for call in calls:
        endpoint = call.pop('endpoint') or ''
        call['endpoint_host'] = urlsplit(endpoint).hostname
        call['region'] = 'cn-beijing' if beijing_endpoint(endpoint) else 'unknown'
    by_kind = []
    for kind in ['extract', 'confirm', 'revise']:
        rows = [t for t in tasks if t['kind'] == kind]
        by_kind.append({'kind': kind, 'recorded_tasks': len(rows), 'statuses': dict(Counter(t['status'] for t in rows)),
                        'attempts': sum(t['attempts'] for t in rows), 'retries': sum(max(0, t['attempts'] - 1) for t in rows),
                        'failure_reasons': dict(Counter(t['error_code'] for t in rows if t['error_code'])),
                        'timed_attempts': sum(e['task_id'] in {t['id'] for t in rows} for e in timed)})
    prices = json.loads((root / 'm12_bundle/prices.json').read_text())
    usage = usage_summary(calls)
    result = {'passed': quality['passed'] and not any(task_rules.values()), 'read_only': True, 'tasks_by_kind': by_kind,
              'quality_passed': quality['passed'], 'task_rules': task_rules, 'usage': usage, 'cost': costs(calls, prices),
              'source_manifest': source, 'external_model_calls_by_this_command': 0,
              'scope': 'Operational task records; observed usage is not proof of account billing. Old M11 attempts have no M12 timing spans; unknowns remain unknown.'}
    output = evidence / 'operational'
    write(output / 'tasks.json', tasks); write(output / 'calls.json', calls)
    write(output / 'attempt_timing.json', timed); write(output / 'summary.json', result)
    write(output / 'quality.json', quality)
    csv_file(output / 'calls.csv', usage['records'], ['id', 'task_id', 'prefix', 'model', 'status', 'attempt',
             'input_tokens', 'output_tokens', 'total_tokens', 'usage_known', 'other_usage', 'error_code'])
    return result


def build(root, evidence, source):
    def get(name):
        path = evidence / name / 'summary.json'
        return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    load = get('load')
    baseline = get('baseline_replay')
    database = get('database')
    real = get('real')
    runtime = get('operational')
    output = evidence / 'analysis'
    output.mkdir(parents=True, exist_ok=True)
    recomputed = []
    recomputation_ok = False
    if load.get('output'):
        raw = root / load['output']
        requests = json.loads((raw / 'requests.json').read_text())
        tasks = json.loads((raw / 'tasks.json').read_text())
        calls = json.loads((raw / 'calls.json').read_text())
        descriptors = [{k: v for k, v in g.items() if k in ['group', 'batch_size', 'concurrency', 'n', 'workload', 'wall_seconds',
                        'passed', 'fixture_sha256', 'task_policy', 'started_monotonic_s', 'ended_monotonic_s']} for g in load['groups']]
        recomputed = summarize_groups(requests, tasks, calls, descriptors)
        recomputation_ok = all(g['unique_tasks'] == old['unique_tasks'] and g['success'] == old['success']
                               and g['usage']['calls'] == old['usage']['calls']
                               and g['end_to_end_ms_success'] == old['end_to_end_ms_success']
                               for g, old in zip(recomputed, load['groups']))
    paired = comparison(recomputed)
    write(output / 'recomputed_groups.json', recomputed)
    write(output / 'comparison.json', paired)
    csv_file(output / 'comparison.csv', paired, ['concurrency', 'baseline_n', 'candidate_n', 'single_median_ms',
             'batch4_median_ms', 'median_change_percent', 'single_calls', 'batch4_calls', 'single_throughput', 'batch4_throughput', 'comparable_correctness'])
    chart(output / 'latency.svg', paired)
    baseline_groups = [g for g in recomputed if g['workload'] == 'independent' and g['batch_size'] == 1]
    categories = {}
    for category in ['model', 'database', 'file_io', 'rate_wait']:
        values = [g['stage_union_ms'][category]['median'] for g in baseline_groups if g['stage_union_ms'][category]['median'] is not None]
        categories[category] = sum(values) / len(values) if values else None
    known_categories = {k: v for k, v in categories.items() if v is not None}
    bottleneck = max(known_categories, key=known_categories.get) if known_categories else 'unknown'
    faster = bool(paired) and all(r['comparable_correctness'] and r['median_change_percent'] is not None and r['median_change_percent'] < 0 for r in paired)
    findings = {
        'observed_largest_measured_category': bottleneck,
        'baseline_category_median_average_ms': categories,
        'batching_faster_in_every_observed_concurrency': faster,
        'deployment_recommendation': 'consider_batch4_after_real_capability_check' if faster and real.get('passed') else 'keep_default1_and_retain_experiment',
        'statistical_significance_tested': False,
        'performance_target_preset': False}
    preserved = (evidence / 'previous_evidence_signature.json').is_file()
    passed = all([load.get('passed'), load.get('full_size_complete'), baseline.get('passed'), database.get('passed'),
                  runtime.get('passed'), recomputation_ok, preserved])
    lines = ['# 健康报告 M12：运行、性能与模型用量分析', '',
             '## 结论', '',
             f'- 受控负载执行：{load.get("independent_requests", "未执行")} 个独立确认请求；重复请求另记 {load.get("duplicate_requests", "未执行")} 个。',
             f'- 单条方案中，已测阶段里耗时最大的一类：`{bottleneck}`。此定位只覆盖本次索引确认负载。',
             f'- 所有并发档位都观察到批量 4 的中位耗时下降：{faster}；未进行统计显著性检验。',
             f'- 建议：`{findings["deployment_recommendation"]}`。未观察到收益时保留结论，默认继续使用 1。', '',
             '## 测量范围与公平性', '',
             '使用合成报告、真实 PostgreSQL 与 pgvector、同一份 FastAPI 路由和任务流水线。受控 HTTP 接口固定每请求延时 20ms，并故意倒序返回带 index 的向量。',
             '主要负载从已准备好的草稿提交确认任务，直到报告及索引 ready；不包含上传和字段抽取。并发 1/3/5 指同时未完成的逻辑任务，处理 worker 始终为 1。',
             '测试 worker 在同一进程的独立线程运行，生产环境每任务子进程启动与监督开销不计入该负载；独立子进程联通在故障验收中单独检查。不能由此推算生产容量。',
             '单因素对照使用同一份代码、合成真值、模型／维度、任务预算和限速；只改变每个请求的输入条数 1→4。各并发档交替 AB/BA 顺序，第一条冷任务计入样本。', '',
             '## 可靠性变化及代价', '',
             f'- 旧 V0 保留，使用其原始 source_snapshot 与 probe 在当前环境重放：{baseline.get("passed", "未执行")}。',
             f'- 相同 fixture、最终业务计数、受控调用数：{baseline.get("fixture_equal")}/{baseline.get("final_counts_equal")}/{baseline.get("controlled_call_counts_equal")}。',
             '- 同步 26 场景的旧／新耗时明细见 baseline_replay 的 overhead.csv；每个场景只有 3 次，报告中位数与差值，不声称总体性能提升。',
             f'- 数据一致性与任务故障回归：{database.get("passed", "未执行")}；逐项场景见 database/summary.json。',
             '- 多版本、幂等和恢复带来额外数据库操作与阶段存储；异步 HTTP 202 接收耗时与任务完成耗时分列。', '',
             '## 单因素优化结果', '',
             '| 未完成任务并发 | 独立任务数：1/4 | 单条中位 ms | 批量4中位 ms | 中位耗时变化 | 调用数：1/4 | 成功吞吐：1/4（任务/s） |',
             '| ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in paired:
        lines.append(f'| {row["concurrency"]} | {row["baseline_n"]}/{row["candidate_n"]} | {number(row["single_median_ms"])} | {number(row["batch4_median_ms"])} | {number(row["median_change_percent"])}% | {row["single_calls"]}/{row["batch4_calls"]} | {number(row["single_throughput"], 3)}/{number(row["batch4_throughput"], 3)} |')
    lines += ['', '![受控索引任务耗时](latency.svg)', '',
              '负载各组的失败率、未完成数、重试、排队和 P95 样本量见 recomputed_groups.json。P95 采用 nearest-rank，样本不足 100 时不显示。重复文件只用于幂等核对，不计入独立处理吞吐。', '',
              '## 阶段与用量口径', '',
              '端到端耗时取提交入库到终态，包含排队和重试；API 接收耗时另列。阶段记录带起点、父 span 和时长；同类区间取并集，嵌套的 processing、model、database 等列不能直接相加。',
              '数据库阶段包含连接、锁等待、事务及提交；模型阶段包含适配器／SDK 时间。进程被强制终止时没有完整 attempt_timing，覆盖率会下降，不补零。',
              '每个 processing_calls 预留和实际可观测 SDK 尝试分别保存。字段缺失、错误返回或中断可能产生 unknown；调用次数减少不等于输入 Token 或费用按同比例减少。', '',
              '## 真实模型测量', '',
              f'- 已记录真实任务数：{real.get("observed_unique_tasks", "未执行")}；完整测量：{real.get("measurement_complete", False)}。',
              '- 本批只测实际配置的 Embedding 索引确认；字段抽取 LLM、视觉接口和问答生成均未测，不代表它们的成本或质量。']
    if real:
        usage, cost = real.get('usage', {}), real.get('cost', {})
        lines += [f'- 可用 usage：{usage.get("known_usage_calls")}/{usage.get("calls")}；未知：{usage.get("unknown_usage_calls")}。',
                  f'- 已知输入 Token 小计：{usage.get("known_input_tokens_subtotal")}；完整输入总数：{usage.get("total_input_tokens")}。',
                  f'- 标准价已知费用小计：{number(cost.get("known_cost_subtotal_yuan"), 6)} 元；完整估算：{number(cost.get("estimated_total_yuan"), 6)} 元。',
                  f'- 价格核对日期：{cost.get("price_checked_at")}；[官方价格]({cost.get("price_source")})。地区和模型不匹配时不套价格；未抵扣免费额度、缓存、优惠，未与账单核对。']
    else:
        lines += ['- 真实 Token 与费用未知。受控接口的 usage 是测试数据，不参与真实费用合计。']
    lines += ['', '## 现有运行记录', '',
              'operational/summary.json 按 extract、confirm、revise 保存现有任务状态、失败原因、重试和用量。旧 M11 任务没有 M12 的完整阶段埋点，缺失仍标未知。']
    for row in runtime.get('tasks_by_kind', []):
        lines.append(f'- `{row["kind"]}`：{row["recorded_tasks"]} 个记录；状态 {json.dumps(row["statuses"], ensure_ascii=False)}；重试 {row["retries"]}；可观测 attempt_timing {row["timed_attempts"]}。')
    lines += ['', '## 边界与复算', '',
              '- 受控固定延时、合成文本长度与本机数据库决定结果；单 worker 与小样本真实模型测量不外推生产并发能力。',
              '- 批量请求错误时整批拒绝并保留 usage，不自动拆成单条重发；重试共用原任务预算。任务保存各自批量大小，旧 M11 阶段按原单条键继续恢复。',
              '- 完整原始请求、任务、调用及 events 保存在各 run；本报告统计由这些 JSON 重算，comparison.csv 与 SVG 取同一份数据。',
              f'- 统计复算一致：{recomputation_ok}；本报告技术门槛：{passed}。真实测量未完成时需要在最终验收明确留空和原因。', '']
    (output / 'M12_ANALYSIS.md').write_text('\n'.join(lines), encoding='utf-8')
    result = {'passed': bool(passed), 'statistics_recomputed': recomputation_ok, 'findings': findings,
              'source_manifest': source, 'report': str((output / 'M12_ANALYSIS.md').relative_to(root)),
              'real_measurement_completed': real.get('passed') is True, 'external_model_calls_by_this_command': 0}
    write(output / 'summary.json', result)
    return result

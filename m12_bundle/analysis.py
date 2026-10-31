"""Recomputable statistics; unknown usage and incomplete tasks are explicit."""

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def distribution(values):
    values = sorted(float(v) for v in values if v is not None)
    n = len(values)
    return {'n': n, 'median': statistics.median(values) if n else None,
            'min': values[0] if n else None, 'max': values[-1] if n else None,
            'p95': values[math.ceil(n * .95) - 1] if n >= 100 else None,
            'p95_method': 'nearest_rank; suppressed when n<100'}


def union_ms(spans, category):
    intervals = sorted((s['start_ms'], s['start_ms'] + s['duration_ms'])
                       for s in spans if s['category'] == category)
    total, end = 0., None
    for start, stop in intervals:
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(end if end is not None else stop, stop)
    return round(total, 3)


def integer(value):
    return value if type(value) is int and value >= 0 else None


def usage_fields(usage, prefix):
    usage = usage if isinstance(usage, dict) else {}
    inp = integer(usage.get('prompt_tokens'))
    source = 'prompt_tokens' if inp is not None else 'unknown'
    if inp is None:
        inp = integer(usage.get('input_tokens'))
        if inp is not None:
            source = 'input_tokens'
    total = integer(usage.get('total_tokens'))
    if prefix == 'EMBEDDING' and inp is None:
        inp = total  # Documented input-only embedding usage, with provenance below.
        if inp is not None:
            source = 'embedding_total_tokens'
    out = 0 if prefix == 'EMBEDDING' else integer(usage.get('completion_tokens'))
    if out is None:
        out = integer(usage.get('output_tokens'))
    return {'input_tokens': inp, 'output_tokens': out, 'total_tokens': total,
            'usage_known': inp is not None and out is not None,
            'input_source': source,
            'other_usage': {k: v for k, v in usage.items() if k not in
                            {'prompt_tokens', 'input_tokens', 'completion_tokens', 'output_tokens', 'total_tokens'}}}


def usage_summary(calls):
    rows = [{**call, **usage_fields(call.get('usage'), call['prefix'])} for call in calls]
    known = [r for r in rows if r['usage_known']]
    return {'calls': len(rows), 'known_usage_calls': len(known), 'unknown_usage_calls': len(rows) - len(known),
            'coverage': len(known) / len(rows) if rows else None,
            'known_input_tokens_subtotal': sum(r['input_tokens'] for r in known),
            'known_output_tokens_subtotal': sum(r['output_tokens'] for r in known),
            'total_input_tokens': sum(r['input_tokens'] for r in known) if rows and len(known) == len(rows) else None,
            'total_output_tokens': sum(r['output_tokens'] for r in known) if rows and len(known) == len(rows) else None,
            'call_states': dict(Counter(r['status'] for r in rows)), 'records': rows}


def task_stats(tasks, wall_seconds):
    terminal = {'succeeded', 'failed', 'superseded'}
    statuses = Counter(t['status'] for t in tasks)
    success = statuses['succeeded']
    failures = sum(t['status'] in {'failed', 'superseded'} for t in tasks)
    return {'unique_tasks': len(tasks), 'success': success, 'failed': failures,
            'unfinished': sum(t['status'] not in terminal for t in tasks),
            'failure_rate': failures / len(tasks) if tasks else None,
            'completion_rate': success / len(tasks) if tasks else None,
            'wall_seconds': wall_seconds,
            'success_throughput_per_second': success / wall_seconds if wall_seconds > 0 else None,
            'end_to_end_ms_all_terminal': distribution(t.get('end_to_end_ms') for t in tasks),
            'end_to_end_ms_success': distribution(t.get('end_to_end_ms') for t in tasks if t['status'] == 'succeeded'),
            'queue_ms': distribution(t.get('initial_queue_ms') for t in tasks),
            'observed_attempts': sum(t.get('timed_attempts', 0) for t in tasks),
            'total_attempts': sum(t['attempts'] for t in tasks),
            'retries': sum(max(t['attempts'] - 1, 0) for t in tasks),
            'failure_reasons': dict(Counter(t.get('error_code') or t['status'] for t in tasks if t['status'] != 'succeeded'))}


def csv_file(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list))
                             else row.get(key) for key in fields})


def summarize_groups(requests, tasks, calls, groups):
    result = []
    for group in groups:
        key = group['group']
        rows = [r for r in requests if r['group'] == key]
        jobs = [t for t in tasks if t['group'] == key]
        usage = usage_summary([c for c in calls if c['group'] == key])
        result.append({**group, **task_stats(jobs, group['wall_seconds']),
                       'submitted_requests': len(rows), 'accepted_requests': sum(r.get('http') == 202 for r in rows),
                       'invalid_or_rejected_requests': sum(r.get('http') != 202 for r in rows),
                       'api_accept_ms': distribution(r.get('accept_ms') for r in rows),
                       'client_completion_ms': distribution(r.get('client_completion_ms') for r in rows),
                       'request_errors': dict(Counter(str(r.get('http')) for r in rows if r.get('http') != 202)),
                       'usage': {k: v for k, v in usage.items() if k != 'records'},
                       'stage_union_ms': {category: distribution(t.get('category_ms', {}).get(category) for t in jobs)
                                          for category in ['database', 'model', 'processing', 'file_io', 'rate_wait']}})
    return result


def comparison(groups):
    paired = defaultdict(dict)
    for g in groups:
        if g['workload'] == 'independent':
            paired[g['concurrency']][g['batch_size']] = g
    rows = []
    for concurrency, sizes in sorted(paired.items()):
        a, b = sizes.get(1), sizes.get(4)
        if a is None or b is None:
            continue
        old, new = a['end_to_end_ms_success']['median'], b['end_to_end_ms_success']['median']
        valid = (a['unfinished'] == b['unfinished'] == a['failed'] == b['failed'] == 0
                 and a.get('fixture_sha256') == b.get('fixture_sha256')
                 and a.get('task_policy') == b.get('task_policy')
                 and a['unique_tasks'] == b['unique_tasks'])
        rows.append({'concurrency': concurrency, 'baseline_n': a['unique_tasks'], 'candidate_n': b['unique_tasks'],
                     'comparable_correctness': valid, 'single_median_ms': old, 'batch4_median_ms': new,
                     'median_change_percent': (new / old - 1) * 100 if old and new is not None else None,
                     'single_calls': a['usage']['calls'], 'batch4_calls': b['usage']['calls'],
                     'single_throughput': a['success_throughput_per_second'],
                     'batch4_throughput': b['success_throughput_per_second']})
    return rows


def chart(path, rows):
    # Standalone deterministic SVG, derived only from actual observations.
    from html import escape
    width, height = 780, max(230, 110 + len(rows) * 85)
    maximum = max([r.get(k) or 0 for r in rows for k in ('single_median_ms', 'batch4_median_ms')] + [1])
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#f7f9fc"/>',
             '<g font-family="Arial,sans-serif" fill="#172b4d">',
             '<text x="30" y="34" font-size="20">Controlled index tasks: median end-to-end latency</text>',
             '<text x="30" y="60" font-size="13">Blue: single input; teal: four inputs/request. Lower is faster. Unit: ms.</text>']
    for index, row in enumerate(rows):
        y = 94 + index * 85
        parts.append(f'<text x="30" y="{y + 14}" font-size="13">C={row["concurrency"]}, n={row["baseline_n"]}/{row["candidate_n"]}</text>')
        for j, (key, color) in enumerate([('single_median_ms', '#3159cc'), ('batch4_median_ms', '#008b86')]):
            value = row.get(key)
            if value is not None:
                bar = value / maximum * 390
                parts.append(f'<rect x="190" y="{y + j * 25}" width="{bar:.2f}" height="18" rx="3" fill="{color}"/>')
                parts.append(f'<text x="{200 + bar:.2f}" y="{y + j * 25 + 14}" font-size="12">{value:.1f}</text>')
    parts.append('</g></svg>')
    Path(path).write_text('\n'.join(parts), encoding='utf-8')

"""Critical ordering, checkpoints, unknown usage, budget and statistical contract tests."""

import copy
import json
import math
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from m12_bundle.analysis import distribution, union_ms, usage_fields, usage_summary, task_stats, comparison
from m12_bundle.cost import estimate, costs, beijing_endpoint
from m12_bundle.budget import Ledger
from m12_bundle.environment import fixture
from app.clients.task_processing import ModelAdapter
from app.domain_errors import BusinessError
from app.task_embedding import plan, prepare, batch_size
from app.task_policy import TaskFailure

PRICES = {'region': 'cn-beijing', 'currency': 'CNY', 'checked_at': '2026-10-31', 'price_source': 'https://official.example',
          'method': 'test_fixture', 'models': {'m': [{'max_input_tokens': 1000, 'input_per_million': 1., 'output_per_million': 0.}]}}


class EmbeddingChecks(unittest.TestCase):
    def response(self, items, usage=None):
        client = MagicMock()
        client.with_options.return_value.embeddings.create.return_value = NS(data=items, usage=NS(model_dump=lambda: usage) if usage is not None else None)
        factory = MagicMock()
        factory.return_value.__enter__.return_value = client
        return factory, client

    def invoke(self, rows, contents=None, usage=None):
        factory, client = self.response(rows, usage)
        with patch.dict(os.environ, {'EMBEDDING_MODEL': 'm', 'EMBEDDING_DIMENSIONS': '2'}), patch('app.clients.task_processing.make_client', factory):
            return ModelAdapter().embeddings(contents or ['a', 'b'], 1.25), client

    def test_transport_order_uses_index(self):
        (result, usage), client = self.invoke([NS(index=1, embedding=[3., 4.]), NS(index=0, embedding=[1., 2.])], usage={'total_tokens': 2})
        self.assertEqual(result['vectors'], [[1., 2.], [3., 4.]])
        client.with_options.assert_called_once_with(timeout=1.25, max_retries=0)
        self.assertEqual(usage, {'total_tokens': 2})

    def test_duplicate_index_rejects_whole_batch(self):
        with self.assertRaises(TaskFailure): self.invoke([NS(index=0, embedding=[1., 2.])] * 2)

    def test_missing_item_rejects_whole_batch(self):
        with self.assertRaises(TaskFailure): self.invoke([NS(index=0, embedding=[1., 2.])])

    def test_out_of_range_index_rejected(self):
        with self.assertRaises(TaskFailure): self.invoke([NS(index=0, embedding=[1., 2.]), NS(index=9, embedding=[3., 4.])])

    def test_wrong_dimension_rejected(self):
        with self.assertRaises(TaskFailure): self.invoke([NS(index=0, embedding=[1.]), NS(index=1, embedding=[3., 4.])])

    def test_non_finite_rejected_and_usage_preserved(self):
        with self.assertRaises(TaskFailure) as caught:
            self.invoke([NS(index=0, embedding=[float('nan'), 2.]), NS(index=1, embedding=[3., 4.])], usage={'total_tokens': 7})
        self.assertEqual(caught.exception.usage, {'total_tokens': 7})

    def test_empty_input_does_not_call_sdk(self):
        with patch('app.clients.task_processing.make_client') as factory:
            with self.assertRaises(TaskFailure): ModelAdapter().embeddings([], 1)
        factory.assert_not_called()

    def test_unknown_usage_is_none(self):
        (result, usage), _ = self.invoke([NS(index=0, embedding=[1., 2.]), NS(index=1, embedding=[3., 4.])])
        self.assertIsNone(usage)

    def test_single_adapter_retains_original_shape(self):
        factory, client = self.response([NS(index=0, embedding=[1., 2.])])
        with patch.dict(os.environ, {'EMBEDDING_MODEL': 'm', 'EMBEDDING_DIMENSIONS': '2'}), patch('app.clients.task_processing.make_client', factory):
            result, usage = ModelAdapter().embedding('one', 2)
        self.assertEqual(result, {'vector': [1., 2.]})


class BatchPreparationChecks(unittest.TestCase):
    def test_only_input_grouping_changes(self):
        text = fixture(1)['raw_text']
        def run(size):
            requested = []
            def embed(contents):
                requested.append(contents)
                return [[float(len(t)), 1.] for t in contents], {'total_tokens': len(contents)}
            result = prepare(text, model='m', dimensions=2, embedder=embed, size=size)
            return result, requested
        a, ra = run(1); b, rb = run(4)
        self.assertEqual(a[0], b[0]); self.assertEqual(a[1:3], b[1:3])
        self.assertGreater(len(ra), len(rb)); self.assertTrue(all(len(r) <= 4 for r in rb))
        self.assertEqual(a[3]['token_usage'], b[3]['token_usage'])

    def test_deduplicates_across_strategies(self):
        chunks, unique = plan('small text')
        self.assertEqual(len(chunks), 2); self.assertEqual(unique, ['small text'])

    def test_positions_and_versions_preserved(self):
        text = fixture(2)['raw_text']
        chunks, _ = plan(text)
        for chunk in chunks:
            self.assertEqual(chunk['content'], text[chunk['start_char']:chunk['end_char']])
        self.assertEqual({c['chunk_version'] for c in chunks}, {'fixed200o40', 'section200o40v1'})

    def test_invalid_batch_size_is_rejected(self):
        for value in [0, 9, True, 1.5, '01', '', None]:
            with patch.dict(os.environ, {'M12_EMBEDDING_BATCH_SIZE': 'bad'}):
                with self.assertRaises(ValueError): batch_size(value)

    def test_missing_batch_size_defaults_to_one(self):
        with patch.dict(os.environ, {}, clear=True): self.assertEqual(batch_size(), 1)

    def test_partial_shape_failure_is_atomic_to_caller(self):
        with self.assertRaises(BusinessError):
            prepare(fixture(1)['raw_text'], model='m', dimensions=2, size=4, embedder=lambda texts: ([[1., 2.]], None))

    def test_unknown_batch_usage_is_not_zero(self):
        result = prepare('small text', model='m', dimensions=2, size=4, embedder=lambda texts: ([[1., 2.]], None))
        self.assertFalse(result[3]['usage_complete']); self.assertEqual(result[3]['token_usage'], {})


class StatisticsChecks(unittest.TestCase):
    def test_nested_intervals_are_not_added_twice(self):
        spans = [{'start_ms': 0, 'duration_ms': 10, 'category': 'db'}, {'start_ms': 2, 'duration_ms': 3, 'category': 'db'},
                 {'start_ms': 8, 'duration_ms': 10, 'category': 'db'}]
        self.assertEqual(union_ms(spans, 'db'), 18)

    def test_small_n_does_not_publish_p95(self):
        self.assertIsNone(distribution([1, 3, 2])['p95'])
        self.assertEqual(distribution([1, 3, 2])['median'], 2)

    def test_p95_nearest_rank(self):
        self.assertEqual(distribution(range(1, 101))['p95'], 95)

    def test_missing_usage_stays_unknown(self):
        self.assertFalse(usage_fields(None, 'LLM')['usage_known'])
        summary = usage_summary([{'prefix': 'LLM', 'status': 'unknown', 'usage': None}])
        self.assertIsNone(summary['total_input_tokens']); self.assertEqual(summary['unknown_usage_calls'], 1)

    def test_embedding_total_tokens_has_explicit_provenance(self):
        for usage in [{'total_tokens': 17}, {'prompt_tokens': None, 'total_tokens': 17}]:
            value = usage_fields(usage, 'EMBEDDING')
            self.assertEqual(value['input_tokens'], 17); self.assertEqual(value['output_tokens'], 0)
            self.assertEqual(value['input_source'], 'embedding_total_tokens')

    def test_generation_requires_output_usage(self):
        self.assertFalse(usage_fields({'prompt_tokens': 17, 'total_tokens': 22}, 'LLM')['usage_known'])

    def test_extra_billing_fields_are_retained(self):
        value = usage_fields({'prompt_tokens': 10, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 3}}, 'LLM')
        self.assertEqual(value['other_usage']['prompt_tokens_details']['cached_tokens'], 3)

    def test_boolean_and_negative_tokens_not_valid_numbers(self):
        for n in [True, -1, '10', 1.1]:
            self.assertFalse(usage_fields({'total_tokens': n}, 'EMBEDDING')['usage_known'])

    def test_no_calls_does_not_imply_measured_zero_tokens(self):
        self.assertIsNone(usage_summary([])['total_input_tokens'])

    def test_unfinished_and_failures_have_separate_denominators(self):
        rows = [{'status': s, 'attempts': 1, 'end_to_end_ms': ms} for s, ms in [('succeeded', 10), ('failed', 20), ('queued', None)]]
        result = task_stats(rows, 2)
        self.assertEqual(result['unfinished'], 1); self.assertEqual(result['failed'], 1)
        self.assertAlmostEqual(result['failure_rate'], 1 / 3); self.assertEqual(result['success_throughput_per_second'], .5)

    def test_incomparable_fixtures_block_comparison(self):
        row = {'workload': 'independent', 'concurrency': 1, 'batch_size': 1, 'unfinished': 0, 'failed': 0, 'unique_tasks': 2,
               'end_to_end_ms_success': {'median': 10}, 'usage': {'calls': 4}, 'success_throughput_per_second': .2, 'fixture_sha256': 'a'}
        result = comparison([row, {**row, 'batch_size': 4, 'fixture_sha256': 'b'}])
        self.assertFalse(result[0]['comparable_correctness'])


class CostAndBudgetChecks(unittest.TestCase):
    def test_unknown_model_region_or_usage_is_not_priced(self):
        call = {'prefix': 'EMBEDDING', 'model': 'm', 'region': 'cn-beijing', 'usage': {'total_tokens': 10}}
        self.assertAlmostEqual(estimate(call, PRICES), .00001)
        for overrides in [{'model': 'other'}, {'region': 'other'}, {'usage': None}]:
            self.assertIsNone(estimate({**call, **overrides}, PRICES))

    def test_cost_subtotal_is_not_unknown_total(self):
        result = costs([{'prefix': 'EMBEDDING', 'model': 'm', 'region': 'cn-beijing', 'usage': None}], PRICES)
        self.assertIsNone(result['estimated_total_yuan'])

    def test_region_check_does_not_match_lookalike_host(self):
        self.assertTrue(beijing_endpoint('https://dashscope.aliyuncs.com/compatible-mode/v1'))
        self.assertFalse(beijing_endpoint('https://dashscope.aliyuncs.com.evil.example/v1'))
        self.assertFalse(beijing_endpoint('https://dashscope-intl.aliyuncs.com/compatible-mode/v1'))

    def test_call_cap_stops_before_new_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / 'ledger.json', PRICES, 1, 1, 'run')
            row = ledger.reserve('m'); ledger.finish(row, {'total_tokens': 5})
            with self.assertRaises(TaskFailure): ledger.reserve('m')
            self.assertEqual(len(ledger.rows), 1)

    def test_money_cap_reserves_maximum_before_sdk(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp) / 'ledger.json', PRICES, .0005, 5, 'run')
            with self.assertRaises(TaskFailure): ledger.reserve('m')
            self.assertEqual(ledger.rows, [])

    def test_unknown_usage_blocks_later_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ledger.json'
            ledger = Ledger(path, PRICES, 1, 5, 'first')
            row = ledger.reserve('m'); ledger.finish(row, None, 'timeout')
            restored = Ledger(path, PRICES, 1, 5, 'second')
            with self.assertRaises(TaskFailure): restored.reserve('m')

    def test_interruption_reservation_is_not_forgotten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ledger.json'
            Ledger(path, PRICES, 1, 5, 'first').reserve('m')
            self.assertTrue(Ledger(path, PRICES, 1, 5, 'second').blocked)

    def test_success_usage_is_persisted_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ledger.json'
            ledger = Ledger(path, PRICES, 1, 5, 'first')
            row = ledger.reserve('m'); ledger.finish(row, {'total_tokens': 9})
            restored = Ledger(path, PRICES, 1, 5, 'second')
            self.assertFalse(restored.blocked); self.assertEqual(len(restored.rows), 1)
            self.assertEqual(restored.rows[0]['usage'], {'total_tokens': 9})


class TimingAndHTTPChecks(unittest.TestCase):
    def test_parent_child_spans_are_persisted_without_business_mutation(self):
        from app.task_metrics import attempt, timed
        repository = MagicMock()
        task = {'id': 'task_synthetic', 'attempts': 1, 'execution_token': 'token'}
        with attempt(repository, task):
            with timed('outer', 'processing'):
                with timed('inner', 'database'): pass
        call = repository.event.call_args
        self.assertEqual(call.args[2], 'attempt_timing')
        spans = call.kwargs['spans']
        outer = next(s for s in spans if s['name'] == 'outer')
        inner = next(s for s in spans if s['name'] == 'inner')
        self.assertEqual(inner['parent_id'], outer['span_id'])
        repository.complete.assert_not_called()

    def test_telemetry_failure_does_not_hide_business_outcome(self):
        from app.task_metrics import attempt
        repository = MagicMock()
        repository.connection_factory.side_effect = OSError('synthetic telemetry failure')
        with attempt(repository, {'id': 'task_synthetic', 'attempts': 1}):
            result = 'business_completed'
        self.assertEqual(result, 'business_completed')

    def test_timing_does_not_swallow_interruption(self):
        from app.task_metrics import attempt
        repository = MagicMock()
        with self.assertRaises(KeyboardInterrupt):
            with attempt(repository, {'id': 'task_synthetic', 'attempts': 1}):
                raise KeyboardInterrupt()
        self.assertEqual(repository.event.call_args.kwargs['outcome'], 'interrupted')

    def test_controlled_http_exercises_real_sdk_ordering(self):
        from m12_bundle.environment import controlled_http, vector_for
        with controlled_http(delay=0) as (endpoint, records):
            with patch.dict(os.environ, {'EMBEDDING_MODEL': 'controlled-m12', 'EMBEDDING_DIMENSIONS': '2',
                                         'EMBEDDING_BASE_URL': endpoint, 'EMBEDDING_API_KEY': 'synthetic-no-secret'}):
                result, usage = ModelAdapter().embeddings(['first', 'second'], 3)
        self.assertEqual(result['vectors'], [vector_for('first', 2), vector_for('second', 2)])
        self.assertEqual(records['requests'], 1)
        self.assertEqual(records['inputs'], 2)
        self.assertIsNotNone(usage['total_tokens'])


class ReportingChecks(unittest.TestCase):
    def setUp(self):
        from m12_bundle.analysis import write, summarize_groups
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / 'evidence'
        self.source = {'fixture': 'synthetic_report_test'}
        descriptors, requests, tasks, calls = [], [], [], []
        for concurrency in [1, 3, 5]:
            for size in [1, 4]:
                group = f'fixture_c{concurrency}_b{size}'
                descriptor = {'group': group, 'concurrency': concurrency, 'batch_size': size, 'workload': 'independent',
                              'n': 2, 'wall_seconds': 1, 'passed': True, 'fixture_sha256': 'same-synthetic-input', 'task_policy': {'max_calls': 128}}
                descriptors.append(descriptor)
                for i in range(2):
                    task_id = group + '_' + str(i)
                    requests.append({'group': group, 'http': 202, 'accept_ms': 1, 'client_completion_ms': 10, 'task_id': task_id})
                    tasks.append({'group': group, 'id': task_id, 'status': 'succeeded', 'attempts': 1,
                                  'end_to_end_ms': 10 if size == 1 else 12, 'initial_queue_ms': 1, 'timed_attempts': 1,
                                  'category_ms': {'database': 2, 'model': 8, 'processing': 9, 'file_io': 0, 'rate_wait': 0}})
                    calls.append({'group': group, 'task_id': task_id, 'prefix': 'EMBEDDING', 'status': 'ok', 'usage': {'total_tokens': 10}})
        self.tasks = tasks
        for name, rows in [('requests', requests), ('tasks', tasks), ('calls', calls)]:
            write(self.root / 'raw' / (name + '.json'), rows)
        groups = summarize_groups(requests, tasks, calls, descriptors)
        write(self.evidence / 'load/summary.json', {'passed': True, 'output': 'raw', 'groups': groups, 'full_size_complete': False,
                                                  'independent_requests': 12, 'duplicate_requests': 0})
        write(self.evidence / 'previous_evidence_signature.json', {'fixture': 'synthetic'})

    def test_report_recomputes_raw_records_and_chart(self):
        from m12_bundle.reporting import build
        result = build(self.root, self.evidence, self.source)
        self.assertTrue(result['statistics_recomputed'])
        self.assertTrue((self.evidence / 'analysis/latency.svg').is_file())
        self.assertFalse(result['passed'])  # Small fixtures cannot complete full acceptance.

    def test_changed_raw_data_fails_recomputation(self):
        from m12_bundle.analysis import write
        from m12_bundle.reporting import build
        self.tasks[0]['end_to_end_ms'] = 999
        write(self.root / 'raw/tasks.json', self.tasks)
        result = build(self.root, self.evidence, self.source)
        self.assertFalse(result['statistics_recomputed']); self.assertFalse(result['passed'])

    def test_negative_result_is_retained_and_real_usage_not_invented(self):
        from m12_bundle.reporting import build
        result = build(self.root, self.evidence, self.source)
        self.assertFalse(result['findings']['batching_faster_in_every_observed_concurrency'])
        self.assertEqual(result['findings']['deployment_recommendation'], 'keep_default1_and_retain_experiment')
        report = (self.evidence / 'analysis/M12_ANALYSIS.md').read_text()
        self.assertIn('真实 Token 与费用未知', report)


if __name__ == '__main__':
    unittest.main()

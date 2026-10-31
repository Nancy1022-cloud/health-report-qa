"""Persistent reservation ledger for the small real embedding experiment."""

import json
import os
import threading
from datetime import datetime, timezone
from uuid import uuid4

from m12_bundle.analysis import write
from m12_bundle.cost import beijing_endpoint, estimate


def configuration(prices):
    from urllib.parse import urlsplit
    model = os.getenv('EMBEDDING_MODEL', '')
    endpoint = os.getenv('EMBEDDING_BASE_URL', '')
    return {'model': model, 'endpoint_host': urlsplit(endpoint).hostname,
            'key_present': bool(os.getenv('EMBEDDING_API_KEY', '').strip()),
            'dimensions': os.getenv('EMBEDDING_DIMENSIONS', '1024'),
            'beijing_endpoint_matches': beijing_endpoint(endpoint),
            'embedding_model_allowed': model in {'qwen3.7-text-embedding-flash', 'qwen3.7-text-embedding', 'text-embedding-v4'},
            'model_price_present': model in prices['models'], 'price_checked_at': prices['checked_at'],
            'price_source': prices['price_source'], 'task_kind': 'confirm/index_only',
            'LLM_calls_planned': 0, 'VISION_calls_planned': 0}


class Ledger:
    def __init__(self, path, prices, budget_yuan, max_calls, run_id):
        self.path, self.prices = path, prices
        self.budget_yuan, self.max_calls, self.run_id = budget_yuan, max_calls, run_id
        self.mutex = threading.Lock()
        self.rows = json.loads(path.read_text(encoding='utf-8'))['entries'] if path.exists() else []
        self.blocked = any(r['state'] in {'reserved', 'unknown'} for r in self.rows)

    def save(self):
        value = {'entries': self.rows, 'budget_yuan': self.budget_yuan,
                 'committed_or_reserved_yuan': sum(r.get('cost_yuan') if r.get('cost_yuan') is not None else r['reserved_yuan'] for r in self.rows),
                 'not_an_account_bill': True}
        temporary = self.path.with_suffix('.tmp')
        write(temporary, value)
        os.replace(temporary, self.path)

    def reserve(self, model):
        from app.task_policy import TaskFailure
        with self.mutex:
            if self.blocked:
                raise TaskFailure('measurement_usage_unknown_stop')
            current = sum(r['run_id'] == self.run_id for r in self.rows)
            if current >= self.max_calls:
                raise TaskFailure('measurement_call_limit')
            tiers = self.prices['models'][model]
            reserve = max(t['max_input_tokens'] * t['input_per_million'] for t in tiers) / 1000000
            spent = sum(r.get('cost_yuan') if r.get('cost_yuan') is not None else r['reserved_yuan'] for r in self.rows)
            if spent + reserve > self.budget_yuan:
                raise TaskFailure('measurement_money_limit')
            row = {'id': uuid4().hex, 'run_id': self.run_id, 'model': model, 'state': 'reserved', 'reserved_yuan': reserve,
                   'created_at': datetime.now(timezone.utc).isoformat(), 'usage': None, 'cost_yuan': None,
                   'meaning': 'SDK attempt reserved; network delivery cannot be inferred from reservation alone'}
            self.rows.append(row)
            self.save()
            return row

    def finish(self, row, usage, error=None):
        with self.mutex:
            value = estimate({'prefix': 'EMBEDDING', 'model': row['model'], 'region': 'cn-beijing', 'usage': usage}, self.prices)
            row.update(usage=usage, cost_yuan=value, error_code=error,
                       state='recorded' if value is not None else 'unknown',
                       finished_at=datetime.now(timezone.utc).isoformat())
            if value is None:
                self.blocked = True
            self.save()


class BudgetAdapter:
    def __init__(self, ledger):
        from app.clients.task_processing import ModelAdapter
        self.base, self.ledger = ModelAdapter(), ledger

    def embeddings(self, contents, timeout):
        model = os.environ['EMBEDDING_MODEL']
        row = self.ledger.reserve(model)
        try:
            result, usage = self.base.embeddings(contents, timeout)
        except Exception as error:
            self.ledger.finish(row, getattr(error, 'usage', None), getattr(error, 'code', type(error).__name__))
            raise
        self.ledger.finish(row, usage)
        return result, usage

    def embedding(self, content, timeout):
        result, usage = self.embeddings([content], timeout)
        return {'vector': result['vectors'][0]}, usage

"""Bounded synchronous embedding microbatches, preserving chunk offsets and version membership."""

import os
import time

from app.chunking import VERSIONS, chunk_text
from app.domain_errors import BusinessError
from app.rag import vector_literal
from app.text_search import tokenize


def batch_size(value=None):
    value = os.getenv('M12_EMBEDDING_BATCH_SIZE', '1') if value is None else value
    if isinstance(value, bool) or str(value) not in {str(n) for n in range(1, 9)}:
        raise ValueError('M12_EMBEDDING_BATCH_SIZE must be an integer in 1..8')
    return int(value)


def plan(raw_text):
    chunks, unique = [], {}
    for strategy, version in VERSIONS.items():
        values = chunk_text(raw_text, strategy)
        if not values:
            raise BusinessError('empty_chunks', '核对文本没有可索引内容。')
        for index, value in enumerate(values):
            unique.setdefault(value['content'], len(unique))
            chunks.append({**value, 'chunk_index': index, 'chunk_version': version})
    return chunks, list(unique)


def prepare(raw_text, *, model, dimensions, embedder, size):
    size = batch_size(size)
    start = time.monotonic()
    chunks, contents = plan(raw_text)
    vectors, totals, calls, complete = {}, {}, 0, True
    for offset in range(0, len(contents), size):
        if time.monotonic() - start > 180:
            raise BusinessError('index_budget_exceeded', '本次建索引耗时超过预算；未确认入库。')
        texts = contents[offset:offset + size]
        values, usage = embedder(texts)
        if len(values) != len(texts) or any(len(v) != dimensions for v in values):
            raise BusinessError('invalid_embedding', 'Embedding 数量或维度不一致；未确认入库。')
        for text, vector in zip(texts, values):
            vector_literal(vector)
            vectors[text] = vector
        calls += 1
        if usage is None:
            complete = False
        for key, value in (usage or {}).items():
            if key.endswith('_tokens') and isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    if time.monotonic() - start > 180:
        raise BusinessError('index_budget_exceeded', '本次建索引耗时超过预算；未确认入库。')
    return ([{**chunk, 'vector': vectors[chunk['content']], 'terms': ' '.join(tokenize(chunk['content']))}
             for chunk in chunks], model, dimensions,
            {'embedding_calls': calls, 'token_usage': totals, 'usage_complete': complete})

"""报告处理 Client 适配：原 M6 索引准备算法，外部 Embedding 通过 callable 注入。"""

import time
from app.chunking import VERSIONS, chunk_text
from app.domain_errors import BusinessError
from app.rag import vector_literal
from app.text_search import tokenize

def prepare_chunks(raw_text, *, config, embedder):
    model, dimensions = config()
    started, cache, prepared, token_usage = time.monotonic(), {}, [], {}
    for strategy, version in VERSIONS.items():
        chunks = chunk_text(raw_text, strategy)
        if not chunks:
            raise BusinessError("empty_chunks", "核对文本没有可索引内容。")
        for index, chunk in enumerate(chunks):
            if time.monotonic() - started > 180:
                raise BusinessError("index_budget_exceeded", "本次建索引耗时超过预算；未确认入库，可缩短报告后重试。")
            content = chunk["content"]
            if content not in cache:
                # 沿用已修复的单条请求，不恢复批量 8 条。
                vectors, usage = embedder([content])
                if len(vectors) != 1 or len(vectors[0]) != dimensions:
                    raise BusinessError("invalid_embedding", "Embedding 返回数量或维度不匹配；未确认入库。")
                vector_literal(vectors[0])
                cache[content] = vectors[0]
                for key, value in usage.items():
                    if key.endswith("_tokens") and isinstance(value, int):
                        token_usage[key] = token_usage.get(key, 0) + value
            prepared.append({**chunk, "chunk_index": index, "chunk_version": version,
                             "vector": cache[content], "terms": " ".join(tokenize(content))})
    if time.monotonic() - started > 180:
        raise BusinessError("index_budget_exceeded", "本次建索引耗时超过预算；未确认入库，可重试。")
    return prepared, model, dimensions, {"embedding_calls": len(cache), "token_usage": token_usage}

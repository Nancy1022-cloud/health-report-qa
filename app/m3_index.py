"""按版本建索引；兼容 M2 的单条 Embedding 请求，并复用内容相同的已有向量。"""

import argparse
import hashlib
import json

from psycopg.rows import dict_row

from app.chunking import VERSIONS, chunk_text
from app.db import connect_db
from app.rag import embed_texts, embedding_config, vector_literal
from app.text_search import SEARCH_VERSION, tokenize


def build_index(chunking="fixed", force_embedding=False):
    version = VERSIONS[chunking]
    model, dimensions = embedding_config()
    with connect_db() as conn:
        conn.row_factory = dict_row
        reports = conn.execute("SELECT id, raw_text FROM reports WHERE status='confirmed' ORDER BY id").fetchall()
        cached = [] if force_embedding else conn.execute(
            "SELECT c.report_id, c.content, c.embedding::text AS vector FROM report_chunks c "
            "JOIN reports r ON r.id=c.report_id WHERE r.status='confirmed' "
            "AND c.embedding_model=%s AND c.embedding_dimensions=%s AND c.embedding IS NOT NULL",
            (model, dimensions),
        ).fetchall()
    cache = {(row["report_id"], row["content"]): json.loads(row["vector"]) for row in cached}
    prepared, total_usage, reused, requested = [], {}, 0, 0
    for report in reports:
        chunks = chunk_text(report["raw_text"], chunking)
        if not chunks:
            raise ValueError("报告没有可索引文本：" + report["id"])
        vectors = []
        for chunk in chunks:
            key = report["id"], chunk["content"]
            if key in cache:
                vector = cache[key]
                reused += 1
            else:
                # 保留 M2 修复：一次只提交一个片段，不恢复成批量 8 条。
                batch, usage = embed_texts([chunk["content"]])
                vector = batch[0]
                requested += 1
                cache[key] = vector
                for name, value in usage.items():
                    if name.endswith("_tokens") and isinstance(value, int):
                        total_usage[name] = total_usage.get(name, 0) + value
            if len(vector) != dimensions:
                raise ValueError("缓存向量维度不匹配。")
            vector_literal(vector)
            vectors.append(vector)
        prepared.append((report, chunks, vectors))
    # 全部向量获取成功后才事务写入；只替换所选版本。
    with connect_db() as conn:
        for report, chunks, vectors in prepared:
            current = conn.execute("SELECT raw_text, status FROM reports WHERE id=%s FOR UPDATE", (report["id"],)).fetchone()
            if current != (report["raw_text"], "confirmed"):
                raise ValueError("建索引期间报告发生变化，本次写入已回滚。")
            conn.execute("DELETE FROM report_chunks WHERE report_id=%s AND chunk_version=%s", (report["id"], version))
            digest = hashlib.sha256(report["raw_text"].encode()).hexdigest()
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                source_id = f"chunk:{report['id']}:{version}:{chunk['start_char']}-{chunk['end_char']}"
                conn.execute(
                    "INSERT INTO report_chunks (id, report_id, chunk_index, section, content, embedding, "
                    "embedding_model, embedding_dimensions, chunk_version, start_char, end_char, source_sha256, "
                    "search_vector, search_version) VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)",
                    (source_id, report["id"], index, chunk["section"], chunk["content"], vector_literal(vector),
                     model, dimensions, version, chunk["start_char"], chunk["end_char"], digest,
                     " ".join(tokenize(chunk["content"])), SEARCH_VERSION),
                )
    return {"status": "ok", "stage": "M3", "chunk_version": version, "reports_indexed": len(prepared),
            "chunks_written": sum(len(chunks) for _, chunks, _ in prepared), "embedding_model": model,
            "dimensions": dimensions, "embedding_calls": requested, "reused_vectors": reused,
            "token_usage": total_usage, "search_version": SEARCH_VERSION}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunking", choices=VERSIONS, default="fixed")
    parser.add_argument("--force-embedding", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_index(args.chunking, args.force_embedding), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
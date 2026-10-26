"""上传草稿存文件；确认后一次事务写入 reports、metrics 和两种版本的片段。"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row

from app.chunking import VERSIONS, chunk_text
from app.db import connect_db
from app.rag import embed_texts, embedding_config, vector_literal
from app.report_extract import extract_report, fail, inspect_file
from app.text_search import SEARCH_VERSION, tokenize
from app.upload_models import Review


def storage_root():
    return Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()


def member_exists(member_id):
    with connect_db() as conn:
        if conn.execute("SELECT id FROM family_members WHERE id=%s", (member_id,)).fetchone() is None:
            fail(404, "member_not_found", "所选成员不存在。")


def draft_path(report_id):
    if not re.fullmatch(r"upload_[a-f0-9]{32}", report_id):
        fail(404, "draft_not_found", "上传草稿不存在。")
    return storage_root() / report_id


def load_draft(report_id, member_id):
    folder = draft_path(report_id)
    try:
        data = json.loads((folder / "draft.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(404, "draft_not_found", "上传草稿不存在。")
    if data["member_id"] != member_id:
        fail(404, "draft_not_found", "该成员下不存在此上传草稿。")
    original = folder / ("source" + data["extension"])
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != data["file_sha256"]:
        fail(409, "source_changed", "上传原件缺失或已改变，请重新上传。")
    return data, original


def create_draft(member_id, filename, data):
    member_exists(member_id)
    ext = inspect_file(data, filename)
    extracted = extract_report(data, ext)
    report_id = "upload_" + uuid4().hex
    folder = draft_path(report_id)
    folder.mkdir(parents=True, exist_ok=False)
    draft = {"id": report_id, "member_id": member_id, "extension": ext,
             "filename": Path(filename.replace("\\", "/")).name[:200],
             "file_sha256": hashlib.sha256(data).hexdigest(), **extracted}
    try:
        (folder / ("source" + ext)).write_bytes(data)
        # 不覆盖首次转录文本；核对后的 raw_text 将单独写入 reports。
        (folder / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(folder)
        raise
    return {**draft, "status": "draft", "file_url": f"/api/ingest/{report_id}/file?member_id={member_id}"}


def existing_review(conn, report_id, member_id):
    row = conn.execute("SELECT title, report_date, report_type, raw_text, member_id, status "
                       "FROM reports WHERE id=%s", (report_id,)).fetchone()
    if row is None:
        return None
    if row["member_id"] != member_id or row["status"] != "confirmed":
        fail(409, "report_conflict", "报告归属或状态冲突，请检查上传 ID。")
    rows = conn.execute("SELECT metric_code, metric_name, value::text AS value, unit, reference_range, source_text "
                        "FROM metrics WHERE report_id=%s AND confirmed=TRUE ORDER BY metric_code", (report_id,)).fetchall()
    return Review(title=row["title"], report_date=row["report_date"], report_type=row["report_type"],
                  raw_text=row["raw_text"], metrics=rows, reviewed=True)


def confirmed_result(report_id, member_id, *, already=False, **extra):
    return {"status": "confirmed", "report_id": report_id, "member_id": member_id,
            "already_confirmed": already, "chunk_versions": list(VERSIONS.values()), **extra}


def check_existing(conn, report_id, member_id, review):
    previous = existing_review(conn, report_id, member_id)
    if previous is None:
        return None
    if previous.canonical() != review.canonical():
        fail(409, "already_confirmed_changed", "此报告已确认；本次提交与已保存内容不同。首版确认后不覆盖，请用新上传修正。")
    return confirmed_result(report_id, member_id, already=True)


def get_draft(report_id, member_id):
    draft, _ = load_draft(report_id, member_id)
    with connect_db() as conn:
        conn.row_factory = dict_row
        previous = existing_review(conn, report_id, member_id)
    result = {**draft, "status": "confirmed" if previous else "draft",
              "extracted_text": draft["raw_text"],
              "file_url": f"/api/ingest/{report_id}/file?member_id={member_id}"}
    if previous:
        result.update(previous.model_dump(mode="json", exclude={"reviewed"}))
    return result


def prepare_chunks(raw_text):
    model, dimensions = embedding_config()
    started, cache, prepared, token_usage = time.monotonic(), {}, [], {}
    for strategy, version in VERSIONS.items():
        chunks = chunk_text(raw_text, strategy)
        if not chunks:
            fail(422, "empty_chunks", "核对文本没有可索引内容。")
        for index, chunk in enumerate(chunks):
            if time.monotonic() - started > 180:
                fail(504, "index_budget_exceeded", "本次建索引耗时超过预算；未确认入库，可缩短报告后重试。")
            content = chunk["content"]
            if content not in cache:
                # 沿用已修复的单条请求，不恢复批量 8 条。
                vectors, usage = embed_texts([content])
                if len(vectors) != 1 or len(vectors[0]) != dimensions:
                    fail(502, "invalid_embedding", "Embedding 返回数量或维度不匹配；未确认入库。")
                vector_literal(vectors[0])
                cache[content] = vectors[0]
                for key, value in usage.items():
                    if key.endswith("_tokens") and isinstance(value, int):
                        token_usage[key] = token_usage.get(key, 0) + value
            prepared.append({**chunk, "chunk_index": index, "chunk_version": version,
                             "vector": cache[content], "terms": " ".join(tokenize(content))})
    if time.monotonic() - started > 180:
        fail(504, "index_budget_exceeded", "本次建索引耗时超过预算；未确认入库，可重试。")
    return prepared, model, dimensions, {"embedding_calls": len(cache), "token_usage": token_usage}


def confirm_report(report_id, member_id, review):
    draft, original = load_draft(report_id, member_id)
    member_exists(member_id)
    with connect_db() as conn:
        conn.row_factory = dict_row
        result = check_existing(conn, report_id, member_id, review)
        if result:
            return result
    # 先完成所有外部调用；失败时 reports / metrics / report_chunks 均不写入。
    chunks, model, dimensions, usage = prepare_chunks(review.raw_text)
    source_hash = hashlib.sha256(review.raw_text.encode()).hexdigest()
    with connect_db() as conn:
        conn.row_factory = dict_row
        # 同一上传 ID 的并发确认在事务内串行，再次检查，防止重复写入。
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (report_id,))
        result = check_existing(conn, report_id, member_id, review)
        if result:
            return result
        conn.execute("INSERT INTO reports (id, member_id, title, report_date, report_type, source_type, "
                     "raw_text, file_path, status, confirmed_at) VALUES (%s,%s,%s,%s,%s,'uploaded',%s,%s,'confirmed',CURRENT_TIMESTAMP)",
                     (report_id, member_id, review.title, review.report_date, review.report_type,
                      review.raw_text, str(original.relative_to(storage_root().parent))))
        for metric in review.metrics:
            conn.execute("INSERT INTO metrics (report_id, metric_code, metric_name, value, unit, reference_range, "
                         "source_text, confirmed) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)",
                         (report_id, metric.metric_code, metric.metric_name, metric.value, metric.unit,
                          metric.reference_range, metric.source_text))
        for chunk in chunks:
            source_id = f"chunk:{report_id}:{chunk['chunk_version']}:{chunk['start_char']}-{chunk['end_char']}"
            conn.execute("INSERT INTO report_chunks (id, report_id, chunk_index, section, content, embedding, "
                         "embedding_model, embedding_dimensions, chunk_version, start_char, end_char, source_sha256, "
                         "search_vector, search_version) VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)",
                         (source_id, report_id, chunk["chunk_index"], chunk["section"], chunk["content"],
                          vector_literal(chunk["vector"]), model, dimensions, chunk["chunk_version"],
                          chunk["start_char"], chunk["end_char"], source_hash, chunk["terms"], SEARCH_VERSION))
    return confirmed_result(report_id, member_id, chunks_written=len(chunks), metrics_written=len(review.metrics), **usage)
"""PostgreSQL 报告存取；连接上下文负责提交/回滚，锁仍保持 M6 的语义。"""

import hashlib
from contextlib import contextmanager

from psycopg.rows import dict_row

from app.domain_errors import BusinessError
from app.rag import vector_literal
from app.text_search import SEARCH_VERSION
from app.upload_models import Review


def existing_review(conn, report_id, member_id):
    row = conn.execute("SELECT title, report_date, report_type, raw_text, member_id, status "
                       "FROM reports WHERE id=%s", (report_id,)).fetchone()
    if row is None:
        return None
    if row["member_id"] != member_id or row["status"] != "confirmed":
        raise BusinessError("report_conflict", "报告归属或状态冲突，请检查上传 ID。")
    rows = conn.execute("SELECT metric_code, metric_name, value::text AS value, unit, reference_range, source_text "
                        "FROM metrics WHERE report_id=%s AND confirmed=TRUE ORDER BY metric_code", (report_id,)).fetchall()
    return Review(title=row["title"], report_date=row["report_date"], report_type=row["report_type"],
                  raw_text=row["raw_text"], metrics=rows, reviewed=True)


class ReportTransaction:
    def __init__(self, conn, review_reader):
        self.conn, self.review_reader = conn, review_reader

    def review(self, report_id, member_id):
        return self.review_reader(self.conn, report_id, member_id)

    def save(self, report_id, member_id, review, file_path, chunks, model, dimensions):
        conn = self.conn
        source_hash = hashlib.sha256(review.raw_text.encode()).hexdigest()
        conn.execute("INSERT INTO reports (id, member_id, title, report_date, report_type, source_type, "
                     "raw_text, file_path, status, confirmed_at) VALUES (%s,%s,%s,%s,%s,'uploaded',%s,%s,'confirmed',CURRENT_TIMESTAMP)",
                     (report_id, member_id, review.title, review.report_date, review.report_type,
                      review.raw_text, file_path))
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


class PostgresReportRepository:
    def __init__(self, connection_factory, review_reader=existing_review):
        self.connection_factory = connection_factory
        self.review_reader = review_reader

    def require_member(self, member_id):
        with self.connection_factory() as conn:
            if conn.execute("SELECT id FROM family_members WHERE id=%s", (member_id,)).fetchone() is None:
                raise BusinessError("member_not_found", "所选成员不存在。")

    def find_review(self, report_id, member_id):
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            return self.review_reader(conn, report_id, member_id)

    @contextmanager
    def confirmation(self, report_id):
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (report_id,))
            yield ReportTransaction(conn, self.review_reader)

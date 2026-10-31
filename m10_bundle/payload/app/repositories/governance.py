"""报告治理持久化：文件元信息、版本投影、不可复用的历史来源和幂等回执。"""

import hashlib
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.data_quality import fingerprint
from app.domain_errors import BusinessError
from app.migrate_m10 import archive_sources
from app.rag import vector_literal
from app.text_search import SEARCH_VERSION
from app.upload_models import Review


def replay(conn, member_id, operation, key, request_fingerprint):
    if key is None:
        return None
    row = conn.execute("SELECT fingerprint,response FROM idempotency_records "
                       "WHERE member_id=%s AND operation=%s AND request_key=%s", (member_id, operation, key)).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != request_fingerprint:
        raise BusinessError("idempotency_conflict", "同一幂等标识已用于不同内容；请核对本次请求。")
    return row["response"]


def receipt(conn, member_id, operation, key, request_fingerprint, response):
    if key is not None:
        conn.execute("INSERT INTO idempotency_records(member_id,operation,request_key,fingerprint,response) "
                     "VALUES (%s,%s,%s,%s,%s)", (member_id, operation, key, request_fingerprint, Jsonb(response)))


def current(conn, report_id, member_id, *, required=False):
    row = conn.execute("SELECT r.id,r.member_id,r.data_version,r.index_version,r.index_state,r.file_path,v.snapshot "
                       "FROM reports r LEFT JOIN report_revisions v ON v.report_id=r.id AND v.version=r.data_version "
                       "WHERE r.id=%s AND r.member_id=%s AND r.status='confirmed'", (report_id, member_id)).fetchone()
    if row is None:
        if required:
            raise BusinessError("report_not_found", "该成员下不存在已确认报告。")
        return None
    if row["snapshot"] is None:
        raise BusinessError("version_conflict", "报告版本记录缺失，请核对数据检查结果。")
    return dict(row)


class GovernanceRepository:
    replay = staticmethod(replay)
    receipt = staticmethod(receipt)
    current = staticmethod(current)

    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def require_member(self, member_id):
        with self.connection_factory() as conn:
            if conn.execute("SELECT id FROM family_members WHERE id=%s", (member_id,)).fetchone() is None:
                raise BusinessError("member_not_found", "所选成员不存在。")

    def find_review(self, report_id, member_id):
        row = self.find_current(report_id, member_id)
        return Review.model_validate({**row["snapshot"], "reviewed": True}) if row else None

    def find_current(self, report_id, member_id, *, required=False):
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            return current(conn, report_id, member_id, required=required)

    @contextmanager
    def upload_lock(self, member_id, file_hash, key):
        """独立会话锁；模型与文件处理期间无写事务。冲突快速返回，可稍后重放。"""
        conn = self.connection_factory()
        try:
            conn.commit()
            conn.autocommit = True
            conn.row_factory = dict_row
            locks = [f"upload-file:{member_id}:{file_hash}"]
            if key:
                locks.append(f"upload-key:{member_id}:{key}")
            for name in sorted(locks):
                if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (name,)).fetchone()["locked"]:
                    raise BusinessError("upload_in_progress", "相同文件或幂等标识正在处理，请稍后重试。")
            yield conn
        finally:
            # 关闭会话释放本次已取得的全部锁，包括第二把锁获取失败的情况。
            conn.close()

    def source(self, conn, member_id, file_hash):
        return conn.execute("SELECT * FROM source_uploads WHERE member_id=%s AND file_sha256=%s", (member_id, file_hash)).fetchone()

    def register_source(self, conn, draft, file_path):
        row = self.source(conn, draft["member_id"], draft["file_sha256"])
        if row:
            if row["id"] != draft["id"]:
                raise BusinessError("duplicate_upload", "该成员已有相同文件，请重新上传以获取已保存的上传 ID。")
            return
        saved = conn.execute("INSERT INTO source_uploads(id,member_id,file_sha256,extension,filename,file_path,extraction) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id", (draft["id"], draft["member_id"], draft["file_sha256"],
                     draft["extension"], draft["filename"], file_path, Jsonb(draft))).fetchone()
        if saved is None:
            matched = self.source(conn, draft["member_id"], draft["file_sha256"])
            if matched is None or matched["id"] != draft["id"]:
                raise BusinessError("duplicate_upload", "原件标识已被使用，请复用已有上传记录或重新核对原件。")

    @contextmanager
    def write(self, report_id, member_id, operation, key):
        with self.connection_factory() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.row_factory = dict_row
            locks = [f"report:{report_id}"]
            if key:
                locks.append(f"write-key:{member_id}:{operation}:{key}")
            for name in sorted(locks):
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (name,))
            yield conn

    def save_version(self, conn, report_id, member_id, review, prepared, file_path, version, actor, reason, origin):
        chunks, model, dimensions, _ = prepared
        snapshot = review.canonical()
        source_hash = hashlib.sha256(review.raw_text.encode()).hexdigest()
        if version == 1:
            conn.execute("INSERT INTO reports(id,member_id,title,report_date,report_type,source_type,raw_text,file_path,status,confirmed_at) "
                         "VALUES (%s,%s,%s,%s,%s,'uploaded',%s,%s,'confirmed',CURRENT_TIMESTAMP)",
                         (report_id, member_id, review.title, review.report_date, review.report_type, review.raw_text, file_path))
            previous_hash = None
        else:
            previous_hash = conn.execute("SELECT snapshot_sha256 FROM report_revisions WHERE report_id=%s AND version=%s",
                                         (report_id, version - 1)).fetchone()["snapshot_sha256"]
            conn.execute("UPDATE reports SET title=%s,report_date=%s,report_type=%s,raw_text=%s,"
                         "data_version=%s,index_version=%s,index_state='ready',confirmed_at=CURRENT_TIMESTAMP "
                         "WHERE id=%s AND member_id=%s", (review.title, review.report_date, review.report_type,
                         review.raw_text, version, version, report_id, member_id))
            conn.execute("DELETE FROM metrics WHERE report_id=%s", (report_id,))
            conn.execute("DELETE FROM report_chunks WHERE report_id=%s", (report_id,))
        conn.execute("INSERT INTO report_revisions(report_id,member_id,version,snapshot,snapshot_sha256,previous_sha256,origin,actor,reason) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (report_id, member_id, version, Jsonb(snapshot),
                     fingerprint(snapshot), previous_hash, origin, actor, reason))
        for metric in review.metrics:
            conn.execute("INSERT INTO metrics(report_id,metric_code,metric_name,value,unit,reference_range,source_text,confirmed,data_version) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)", (report_id, metric.metric_code, metric.metric_name,
                         metric.value, metric.unit, metric.reference_range, metric.source_text, version))
        for chunk in chunks:
            sid = f"chunk:{report_id}:v{version}:{chunk['chunk_version']}:{chunk['start_char']}-{chunk['end_char']}"
            conn.execute("INSERT INTO report_chunks(id,report_id,chunk_index,section,content,embedding,embedding_model,embedding_dimensions,"
                         "chunk_version,start_char,end_char,source_sha256,search_vector,search_version,data_version) "
                         "VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s,%s)",
                         (sid, report_id, chunk["chunk_index"], chunk["section"], chunk["content"], vector_literal(chunk["vector"]),
                          model, dimensions, chunk["chunk_version"], chunk["start_char"], chunk["end_char"], source_hash,
                          chunk["terms"], SEARCH_VERSION, version))
        archive_sources(conn, report_id, version)
        if version > 1:
            # 旧问答保留作历史记录，明确标记来源已修订；它们不作为问答缓存复用。
            conn.execute("UPDATE query_logs SET stale_report_ids=array_append(stale_report_ids,%s) "
                         "WHERE NOT (%s=ANY(stale_report_ids)) AND (citations @> %s OR trace @> %s)",
                         (report_id, report_id, Jsonb([{"report_id": report_id}]), Jsonb([{"report_ids": [report_id]}])))

    def provenance(self, report_id, member_id):
        with self.connection_factory() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.row_factory = dict_row
            row = current(conn, report_id, member_id, required=True)
            source = conn.execute("SELECT id,file_sha256,filename,extension,extraction,created_at FROM source_uploads "
                                  "WHERE id=%s AND member_id=%s", (report_id, member_id)).fetchone()
            revisions = conn.execute("SELECT version AS report_version,origin,actor,reason,created_at,snapshot_sha256,previous_sha256,snapshot "
                                     "FROM report_revisions WHERE report_id=%s AND member_id=%s ORDER BY version", (report_id, member_id)).fetchall()
            for revision in revisions:
                revision["metric_sources"] = [r["source_id"] for r in conn.execute("SELECT source_id FROM revision_sources "
                    "WHERE report_id=%s AND version=%s AND source_kind='metric' ORDER BY source_id", (report_id, revision["report_version"])).fetchall()]
        return {"report_id": report_id, "member_id": member_id, "current_version": row["data_version"],
                "index_version": row["index_version"], "index_state": row["index_state"],
                "original_source": dict(source) if source else None, "original_extraction_available": source is not None,
                "revisions": [dict(r) for r in revisions]}

    def resolve_source(self, source_id, member_id):
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            row = conn.execute("SELECT s.*,r.member_id,r.data_version AS current_version FROM revision_sources s "
                               "JOIN reports r ON r.id=s.report_id WHERE s.source_id=%s AND r.member_id=%s", (source_id, member_id)).fetchone()
        if row is None:
            raise BusinessError("source_not_found", "该成员下不存在此来源。")
        return {"source_id": source_id, "report_id": row["report_id"], "member_id": member_id,
                "report_version": row["version"], "current_version": row["current_version"],
                "is_current": row["version"] == row["current_version"], "source_kind": row["source_kind"],
                "payload": row["payload"], "provenance_url": f"/api/reports/{row['report_id']}/provenance?member_id={member_id}"}

    def version_snapshot(self, member_ids, report_ids=None):
        if not member_ids:
            return {}
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            rows = conn.execute("SELECT id,data_version FROM reports WHERE member_id=ANY(%s::text[]) AND status='confirmed' "
                                "AND (%s::text[] IS NULL OR id=ANY(%s::text[])) ORDER BY id", (member_ids, report_ids, report_ids)).fetchall()
        return {r["id"]: r["data_version"] for r in rows}

    def invalidate_query(self, query_id, report_ids):
        if query_id:
            with self.connection_factory() as conn:
                conn.execute("UPDATE query_logs SET error_code='version_conflict',stale_report_ids="
                             "ARRAY(SELECT DISTINCT unnest(stale_report_ids || %s::text[])) WHERE id=%s", (report_ids, query_id))

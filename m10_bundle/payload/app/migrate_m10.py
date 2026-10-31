"""M10 单事务增量迁移。先在数据副本演练；校验和不匹配时停止。"""

import hashlib
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.data_quality import fingerprint
from app.domain_errors import BusinessError
from app.repositories.reports import existing_review

VERSION = "health_m10_v1"
DDL = """
ALTER TABLE reports ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0);
ALTER TABLE reports ADD COLUMN index_version INTEGER NOT NULL DEFAULT 1 CHECK (index_version > 0);
ALTER TABLE reports ADD COLUMN index_state TEXT NOT NULL DEFAULT 'ready' CHECK (index_state IN ('ready','invalid'));
ALTER TABLE reports ADD CONSTRAINT reports_member_identity UNIQUE (id, member_id);
ALTER TABLE metrics ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0);
ALTER TABLE report_chunks ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0);
CREATE TABLE source_uploads (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES family_members(id),
    file_sha256 TEXT NOT NULL CHECK (file_sha256 ~ '^[a-f0-9]{64}$'),
    extension TEXT NOT NULL CHECK (extension IN ('.pdf','.png','.jpg')),
    filename TEXT NOT NULL, file_path TEXT NOT NULL,
    extraction JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (member_id, file_sha256)
);
CREATE TABLE report_revisions (
    report_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    snapshot JSONB NOT NULL,
    snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^[a-f0-9]{64}$'),
    previous_sha256 TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('confirmation','revision','legacy_snapshot')),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''), reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (report_id, version),
    FOREIGN KEY (report_id, member_id) REFERENCES reports(id, member_id)
);
CREATE TABLE revision_sources (
    source_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL, version INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('metric','chunk','report')),
    payload JSONB NOT NULL,
    FOREIGN KEY (report_id, version) REFERENCES report_revisions(report_id, version)
);
CREATE TABLE idempotency_records (
    member_id TEXT NOT NULL REFERENCES family_members(id),
    operation TEXT NOT NULL CHECK (operation IN ('upload','confirm','revise')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, operation, request_key)
);
ALTER TABLE query_logs ADD COLUMN stale_report_ids TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX idx_revision_sources_report ON revision_sources(report_id, version);
"""


def checksum():
    # 包含回填实现；修改已部署迁移需要新版本，不覆盖同名迁移。
    return hashlib.sha256(Path(__file__).read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode()).hexdigest()


def migrate(connection_factory, drafts=None):
    with connection_factory() as conn:
        conn.row_factory = dict_row
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_m10_migration'))")
        conn.execute("CREATE TABLE IF NOT EXISTS engineering_migrations "
                     "(version TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        previous = conn.execute("SELECT sha256 FROM engineering_migrations WHERE version=%s", (VERSION,)).fetchone()
        if previous:
            if previous["sha256"] != checksum():
                raise RuntimeError("M10 迁移校验和不一致，停止；不要删除迁移记录。")
            return {"passed": True, "already_applied": True, "version": VERSION}
        required = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                                "AND table_name='report_chunks'").fetchall()
        if not {"chunk_version", "source_sha256", "search_version"} <= {r["column_name"] for r in required}:
            raise RuntimeError("缺少 M3 索引字段，先核对 M9 数据库。")
        conn.execute(DDL)
        rows = conn.execute("SELECT id, member_id FROM reports WHERE status='confirmed' ORDER BY id").fetchall()
        registered_originals = 0
        unavailable_originals = 0
        for row in rows:
            review = existing_review(conn, row["id"], row["member_id"])
            snapshot = review.canonical()
            conn.execute("INSERT INTO report_revisions(report_id,member_id,version,snapshot,snapshot_sha256,origin,actor,reason) "
                         "VALUES (%s,%s,1,%s,%s,'legacy_snapshot','migration','M10 前已确认数据；首次抽取未在本次迁移中补造')",
                         (row["id"], row["member_id"], Jsonb(snapshot), fingerprint(snapshot)))
            archive_sources(conn, row["id"], 1)
            if drafts is not None and row["id"].startswith("upload_"):
                try:
                    original_draft, original_file = drafts.load(row["id"], row["member_id"])
                except (BusinessError, OSError, ValueError, KeyError):
                    unavailable_originals += 1
                else:
                    saved = conn.execute("INSERT INTO source_uploads(id,member_id,file_sha256,extension,filename,file_path,extraction) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id", (row["id"], row["member_id"],
                        original_draft["file_sha256"], original_draft["extension"], original_draft["filename"],
                        drafts.relative_original(original_file), Jsonb(original_draft))).fetchone()
                    registered_originals += int(saved is not None)
                    unavailable_originals += int(saved is None)
        conn.execute("ALTER TABLE metrics ADD CONSTRAINT metrics_report_version_fk FOREIGN KEY(report_id,data_version) "
                     "REFERENCES report_revisions(report_id,version)")
        conn.execute("ALTER TABLE report_chunks ADD CONSTRAINT chunks_report_version_fk FOREIGN KEY(report_id,data_version) "
                     "REFERENCES report_revisions(report_id,version)")
        conn.execute("ALTER TABLE reports ADD CONSTRAINT ready_index_matches_version CHECK (index_state <> 'ready' OR index_version=data_version)")
        conn.execute("INSERT INTO engineering_migrations(version,sha256) VALUES (%s,%s)", (VERSION, checksum()))
    return {"passed": True, "already_applied": False, "version": VERSION, "legacy_reports_snapshotted": len(rows),
            "registered_legacy_originals": registered_originals, "unavailable_or_duplicate_legacy_originals": unavailable_originals}


def archive_sources(conn, report_id, version):
    for row in conn.execute("SELECT id,metric_code,metric_name,value::text AS value,unit,reference_range,source_text "
                            "FROM metrics WHERE report_id=%s AND confirmed=TRUE ORDER BY id", (report_id,)).fetchall():
        conn.execute("INSERT INTO revision_sources(source_id,report_id,version,source_kind,payload) VALUES (%s,%s,%s,'metric',%s)",
                     (f"metric:{row['id']}", report_id, version, Jsonb(dict(row))))
    for row in conn.execute("SELECT id,content,chunk_version,start_char,end_char,source_sha256 FROM report_chunks WHERE report_id=%s",
                            (report_id,)).fetchall():
        conn.execute("INSERT INTO revision_sources(source_id,report_id,version,source_kind,payload) VALUES (%s,%s,%s,'chunk',%s)",
                     (row["id"], report_id, version, Jsonb(dict(row))))
    row = conn.execute("SELECT title,report_date::text AS report_date,report_type FROM reports WHERE id=%s", (report_id,)).fetchone()
    conn.execute("INSERT INTO revision_sources(source_id,report_id,version,source_kind,payload) VALUES (%s,%s,%s,'report',%s)",
                 (f"report:{report_id}:v{version}", report_id, version, Jsonb(dict(row))))

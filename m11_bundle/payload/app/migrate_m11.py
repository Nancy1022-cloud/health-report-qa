"""M11 只新增任务相关表；不重写 M10 业务记录。"""

import hashlib
from pathlib import Path

from psycopg.rows import dict_row

VERSION = 'health_m11_v1'
DDL = """
CREATE TABLE processing_tasks (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES family_members(id),
    report_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('extract','confirm','revise')),
    generation INTEGER NOT NULL CHECK (generation > 0),
    fingerprint TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','superseded')),
    stage TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 5),
    calls_reserved INTEGER NOT NULL DEFAULT 0 CHECK (calls_reserved >= 0),
    max_calls INTEGER NOT NULL CHECK (max_calls BETWEEN 1 AND 256),
    deadline TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_until TIMESTAMPTZ,
    execution_token TEXT,
    result JSONB,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    CHECK (calls_reserved <= max_calls),
    CHECK (attempts <= max_attempts),
    CHECK ((status = 'running') = (execution_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX idx_processing_claim ON processing_tasks(available_at,created_at) WHERE status='queued';
CREATE INDEX idx_processing_file ON processing_tasks(member_id,(payload->>'file_sha256')) WHERE kind='extract';
CREATE TABLE processing_heads (
    member_id TEXT NOT NULL REFERENCES family_members(id), report_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0), task_id TEXT NOT NULL REFERENCES processing_tasks(id),
    PRIMARY KEY(member_id,report_id)
);
CREATE TABLE processing_receipts (
    member_id TEXT NOT NULL REFERENCES family_members(id), kind TEXT NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES processing_tasks(id),
    PRIMARY KEY(member_id,kind,request_key)
);
CREATE TABLE processing_artifacts (
    task_id TEXT NOT NULL REFERENCES processing_tasks(id), stage TEXT NOT NULL,
    fingerprint TEXT NOT NULL, result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(task_id,stage)
);
CREATE TABLE processing_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES processing_tasks(id), event TEXT NOT NULL,
    attempt INTEGER NOT NULL, execution_token TEXT, detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE processing_calls (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES processing_tasks(id),
    attempt INTEGER NOT NULL, execution_token TEXT NOT NULL, stage TEXT NOT NULL, prefix TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved','ok','error','unknown')),
    usage JSONB, error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ
);
CREATE TABLE processing_worker_state (
    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat_at TIMESTAMPTZ,
    next_call_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), worker_id TEXT
);
INSERT INTO processing_worker_state(id) VALUES (1);
"""


def checksum():
    return hashlib.sha256(Path(__file__).read_text(encoding='utf-8-sig').replace('\r\n', '\n').encode()).hexdigest()


def migrate(connection_factory):
    with connection_factory() as conn:
        conn.row_factory = dict_row
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('health_m11_migration'))")
        if conn.execute("SELECT to_regclass('engineering_migrations') AS t").fetchone()['t'] is None:
            raise RuntimeError('先完成 M10 数据库迁移')
        if not conn.execute("SELECT 1 FROM engineering_migrations WHERE version='health_m10_v1'").fetchone():
            raise RuntimeError('未登记 M10 数据库迁移')
        row = conn.execute('SELECT sha256 FROM engineering_migrations WHERE version=%s', (VERSION,)).fetchone()
        if row:
            if row['sha256'] != checksum():
                raise RuntimeError('M11 迁移校验和改变；请保留记录并使用新迁移修复')
            return {'passed': True, 'already_applied': True, 'version': VERSION}
        conn.execute(DDL)
        conn.execute('INSERT INTO engineering_migrations(version,sha256) VALUES (%s,%s)', (VERSION, checksum()))
    return {'passed': True, 'already_applied': False, 'version': VERSION}

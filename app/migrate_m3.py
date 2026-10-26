"""给 M1/M2 的片段表补充版本和位置字段，不删除报告、指标或旧片段。"""

import hashlib
import json
import re

from psycopg.rows import dict_row

from app.db import connect_db
from app.chunking import VERSIONS


def migrate():
    with connect_db() as conn:
        conn.execute("ALTER TABLE report_chunks ADD COLUMN IF NOT EXISTS chunk_version TEXT")
        conn.execute("ALTER TABLE report_chunks ADD COLUMN IF NOT EXISTS start_char INTEGER")
        conn.execute("ALTER TABLE report_chunks ADD COLUMN IF NOT EXISTS end_char INTEGER")
        conn.execute("ALTER TABLE report_chunks ADD COLUMN IF NOT EXISTS source_sha256 TEXT")
        conn.execute("ALTER TABLE report_chunks ADD COLUMN IF NOT EXISTS search_version TEXT")
        conn.row_factory = dict_row
        rows = conn.execute(
            "SELECT c.*, r.raw_text FROM report_chunks c JOIN reports r ON r.id = c.report_id "
            "WHERE c.chunk_version IS NULL OR c.start_char IS NULL OR c.end_char IS NULL OR c.source_sha256 IS NULL"
        ).fetchall()
        for row in rows:
            match = re.fullmatch(r"chunk:(.+):([^:]+):(\d+)-(\d+)", row["id"])
            if not match or match.group(1) != row["report_id"] or match.group(2) not in VERSIONS.values():
                raise ValueError("旧片段 ID 不是 M2 约定格式，迁移已回滚：" + row["id"])
            start, end = int(match.group(3)), int(match.group(4))
            if not 0 <= start < end <= len(row["raw_text"]) or row["raw_text"][start:end] != row["content"]:
                raise ValueError("旧片段与原文不一致，迁移已回滚：" + row["id"])
            digest = hashlib.sha256(row["raw_text"].encode()).hexdigest()
            conn.execute(
                "UPDATE report_chunks SET chunk_version=%s, start_char=%s, end_char=%s, source_sha256=%s WHERE id=%s",
                (match.group(2), start, end, digest, row["id"]),
            )
        for column in ("chunk_version", "start_char", "end_char", "source_sha256"):
            # column 是上面的固定常量，不接收外部输入。
            conn.execute(f"ALTER TABLE report_chunks ALTER COLUMN {column} SET NOT NULL")
        conn.execute("ALTER TABLE report_chunks DROP CONSTRAINT IF EXISTS report_chunks_report_id_chunk_index_key")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_version_position ON report_chunks (report_id, chunk_version, chunk_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_search_vector ON report_chunks USING GIN (search_vector)")
    return {"status": "ok", "old_chunks_backfilled": len(rows), "stage": "M3"}


if __name__ == "__main__":
    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
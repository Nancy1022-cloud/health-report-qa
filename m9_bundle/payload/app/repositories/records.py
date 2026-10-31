"""只读 SQL；连接由 FastAPI 依赖提供，也可在独立调用中显式传入。"""


class RecordRepository:
    def __init__(self, conn):
        self.conn = conn

    def member(self, member_id):
        return self.conn.execute("SELECT id, name, aliases FROM family_members WHERE id = %s", (member_id,)).fetchone()

    def members(self):
        return self.conn.execute("SELECT id, name, aliases FROM family_members ORDER BY id").fetchall()

    def ingest_members(self):
        return self.conn.execute("SELECT id, name FROM family_members ORDER BY id").fetchall()

    def reports(self, member_id):
        return self.conn.execute(
            "SELECT id, member_id, title, report_date, report_type, source_type, status "
            "FROM reports WHERE member_id = %s AND status = 'confirmed' ORDER BY report_date, id",
            (member_id,),
        ).fetchall()

    def report(self, report_id, member_id):
        return self.conn.execute(
            "SELECT id, member_id, title, report_date, report_type, source_type, status, raw_text "
            "FROM reports WHERE id = %s AND member_id = %s AND status = 'confirmed'",
            (report_id, member_id),
        ).fetchone()

    def metrics(self, member_id, metric_code, date_from=None, date_to=None):
        sql = (
            "SELECT m.id, m.report_id, r.member_id, r.report_date, m.metric_code, m.metric_name, "
            "m.value::text AS value, m.unit, m.reference_range, m.source_text "
            "FROM metrics m JOIN reports r ON r.id = m.report_id "
            "WHERE r.member_id = %s AND r.status = 'confirmed' AND m.confirmed = TRUE"
        )
        params = [member_id]
        if metric_code is not None:
            sql += " AND m.metric_code = %s"
            params.append(metric_code)
        if date_from is not None:
            sql += " AND r.report_date >= %s"
            params.append(date_from)
        if date_to is not None:
            sql += " AND r.report_date <= %s"
            params.append(date_to)
        return self.conn.execute(sql + " ORDER BY r.report_date, r.id, m.metric_code", params).fetchall()

    def index_stats(self):
        return self.conn.execute(
            "SELECT r.member_id, r.id AS report_id, r.report_date, c.chunk_version, "
            "count(c.id) AS chunks, count(c.embedding) AS vectors, count(c.search_vector) AS keyword_vectors, "
            "min(c.embedding_model) AS embedding_model, min(c.embedding_dimensions) AS dimensions "
            "FROM reports r LEFT JOIN report_chunks c ON c.report_id=r.id "
            "WHERE r.status='confirmed' GROUP BY r.id, c.chunk_version ORDER BY r.member_id, r.report_date, r.id, c.chunk_version"
        ).fetchall()

    def trace(self, query_id):
        return self.conn.execute("SELECT * FROM query_logs WHERE id = %s", (query_id,)).fetchone()

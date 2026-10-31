"""成员/报告范围和响应组装，不含 SQL 或 HTTP 对象。"""

from app.domain_errors import BusinessError
from app.observability import observed


class RecordService:
    def __init__(self, repository):
        self.repository = repository

    @observed("records.require_member")
    def require_member(self, member_id):
        member = self.repository.member(member_id)
        if member is None:
            raise BusinessError("member_not_found", "成员不存在。")
        return member

    @observed("records.members")
    def members(self):
        rows = self.repository.members()
        return {"items": rows, "count": len(rows)}

    @observed("records.ingest_members")
    def ingest_members(self):
        return self.repository.ingest_members()

    @observed("records.reports")
    def reports(self, member_id):
        member = self.require_member(member_id)
        rows = self.repository.reports(member_id)
        return {"member": member, "items": rows, "count": len(rows), "result_code": "ok" if rows else "no_records"}

    @observed("records.report")
    def report(self, report_id, member_id):
        self.require_member(member_id)
        report = self.repository.report(report_id, member_id)
        if report is None:
            raise BusinessError("report_not_found", "该成员下没有这份已确认报告。")
        return report

    @observed("records.metrics")
    def metrics(self, member_id, metric_code=None, date_from=None, date_to=None):
        member = self.require_member(member_id)
        rows = (self.repository.metrics(member_id, metric_code) if date_from is None and date_to is None
                else self.repository.metrics(member_id, metric_code, date_from, date_to))
        for row in rows:
            row["source_id"] = f"metric:{row['id']}"
        return {"member": member, "items": rows, "count": len(rows), "result_code": "ok" if rows else "no_records"}

    @observed("records.index_stats")
    def index_stats(self):
        rows = self.repository.index_stats()
        return {"items": rows, "report_count": len({r["report_id"] for r in rows}),
                "chunk_count": sum(r["chunks"] for r in rows)}

    @observed("records.trace")
    def trace(self, query_id):
        row = self.repository.trace(query_id)
        if row is None:
            raise BusinessError("trace_not_found", "未找到这条问答日志。")
        return row

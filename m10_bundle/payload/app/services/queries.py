"""问答入口的编排适配；M5 的工具循环和证据算法继续复用。"""


from app.observability import observed, business_result
from app.domain_errors import BusinessError


class QueryService:
    def __init__(self, query_runner, agent_runner, version_reader=None, version_change_handler=None):
        self.query_runner, self.agent_runner = query_runner, agent_runner
        self.version_reader = version_reader
        self.version_change_handler = version_change_handler

    def _snapshot(self, members, reports):
        return self.version_reader(members, reports) if self.version_reader else None

    def _unchanged(self, previous, members, reports, result):
        if self.version_reader:
            after = self._snapshot(members, reports)
            if previous != after:
                if self.version_change_handler:
                    changed = sorted(r for r in set(previous) | set(after) if previous.get(r) != after.get(r))
                    self.version_change_handler(result.get("query_id"), changed)
                raise BusinessError("version_conflict", "查询期间报告发生修订，请按最新版本重试。")

    @observed("query.pipeline")
    def query(self, request):
        before = self._snapshot([request.member_id], request.report_ids)
        result = self.query_runner(request.question, request.member_id, request.report_ids, request.top_k,
                                 with_answer=request.with_answer, chunk_version=request.chunk_version,
                                 retrieval_mode=request.retrieval_mode)
        result = business_result(result, no_records=result.get("result_code") == "no_records")
        self._unchanged(before, [request.member_id], request.report_ids, result)
        return result

    @observed("agent.pipeline")
    async def agent_query(self, request):
        before = self._snapshot(request.member_ids, request.report_ids)
        result = await self.agent_runner(request.question, request.member_ids, request.report_ids,
                                       chunk_version=request.chunk_version, retrieval_mode=request.retrieval_mode)
        result = business_result(result, no_records=result.get("result_code") == "no_records")
        self._unchanged(before, request.member_ids, request.report_ids, result)
        return result

"""问答入口的编排适配；M5 的工具循环和证据算法继续复用。"""


class QueryService:
    def __init__(self, query_runner, agent_runner):
        self.query_runner, self.agent_runner = query_runner, agent_runner

    def query(self, request):
        return self.query_runner(request.question, request.member_id, request.report_ids, request.top_k,
                                 with_answer=request.with_answer, chunk_version=request.chunk_version,
                                 retrieval_mode=request.retrieval_mode)

    async def agent_query(self, request):
        return await self.agent_runner(request.question, request.member_ids, request.report_ids,
                                       chunk_version=request.chunk_version, retrieval_mode=request.retrieval_mode)

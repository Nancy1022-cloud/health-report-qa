"""M8 新增分层检查：不访问真实模型或数据库。"""

import ast
import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent_routes, query_routes
from app.dependencies import get_query_service
from app.domain_errors import BusinessError
from app.repositories.drafts import DraftRepository
from app.schemas.queries import AgentRequest, QueryRequest
from app.services.queries import QueryService
from app.services.records import RecordService
from app.services.reports import ReportService
from app.upload_models import Review

TEXT = "空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。"
BODY = {"title": "M8 合成报告", "report_date": "2026-10-30", "report_type": "体检报告",
        "raw_text": TEXT, "reviewed": True, "metrics": [{"metric_code": "fasting_glucose",
        "metric_name": "空腹血糖", "value": "5.7", "unit": "mmol/L",
        "reference_range": "3.9-6.1", "source_text": TEXT}]}


class MemoryReports:
    """只用于验证业务编排；不把此替身的通过视为 PostgreSQL 事务证据。"""
    def __init__(self):
        self.previous = None
        self.after_prepare = None
        self.events = []
        self.saved = []

    def require_member(self, member):
        if member != "a":
            raise BusinessError("member_not_found", "不存在")
        self.events.append("member")

    def find_review(self, *_):
        self.events.append("read")
        return self.previous

    @contextmanager
    def confirmation(self, report_id):
        self.events.append("transaction_enter")
        yield NS(review=lambda *_: self.after_prepare,
                 save=lambda *args: self.saved.append(args))
        self.events.append("transaction_exit")


class ReportServiceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = MemoryReports()
        self.drafts = DraftRepository(lambda: Path(self.temp.name))
        self.draft = self.drafts.create("a", "demo.png", b"fixture", ".png", {"raw_text": TEXT})
        self.report_id = self.draft["id"]
        self.review = Review.model_validate(BODY)
        def index(text):
            self.repo.events.append("external_index")
            return [], "controlled", 3, {"embedding_calls": 1, "token_usage": {}}
        self.indexer = Mock(side_effect=index)
        self.service = ReportService(self.repo, self.drafts, Mock(return_value=".png"),
                                     Mock(return_value={"raw_text": TEXT}), self.indexer)

    def test_external_index_runs_before_write_transaction(self):
        result = self.service.confirm(self.report_id, "a", self.review)
        self.assertFalse(result["already_confirmed"])
        self.assertLess(self.repo.events.index("external_index"), self.repo.events.index("transaction_enter"))
        self.assertEqual(len(self.repo.saved), 1)

    def test_same_review_skips_index_and_write(self):
        self.repo.previous = self.review
        result = self.service.confirm(self.report_id, "a", self.review)
        self.assertTrue(result["already_confirmed"])
        self.indexer.assert_not_called()
        self.assertEqual(self.repo.saved, [])

    def test_changed_review_has_business_error(self):
        self.repo.previous = self.review
        with self.assertRaises(BusinessError) as ctx:
            self.service.confirm(self.report_id, "a", Review.model_validate({**BODY, "title": "新标题"}))
        self.assertEqual(ctx.exception.code, "already_confirmed_changed")
        self.indexer.assert_not_called()

    def test_second_check_reuses_result_created_during_indexing(self):
        self.repo.after_prepare = self.review
        result = self.service.confirm(self.report_id, "a", self.review)
        self.assertTrue(result["already_confirmed"])
        self.assertEqual(self.repo.saved, [])
        self.indexer.assert_called_once()

    def test_second_check_rejects_conflict(self):
        self.repo.after_prepare = Review.model_validate({**BODY, "title": "已确认的新标题"})
        with self.assertRaises(BusinessError):
            self.service.confirm(self.report_id, "a", self.review)
        self.assertEqual(self.repo.saved, [])

    def test_index_error_never_enters_write_transaction(self):
        self.indexer.side_effect = RuntimeError("controlled failure")
        with self.assertRaises(RuntimeError):
            self.service.confirm(self.report_id, "a", self.review)
        self.assertNotIn("transaction_enter", self.repo.events)

    def test_draft_scope_precedes_external_call(self):
        with self.assertRaises(BusinessError) as ctx:
            self.service.confirm(self.report_id, "b", self.review)
        self.assertEqual(ctx.exception.code, "draft_not_found")
        self.indexer.assert_not_called()

    def test_member_check_precedes_extract(self):
        with self.assertRaises(BusinessError):
            self.service.create_draft("missing", "a.png", b"data")
        self.service.extractor.assert_not_called()

    def test_changed_source_rejected(self):
        (self.drafts.path(self.report_id) / "source.png").write_bytes(b"changed")
        with self.assertRaises(BusinessError) as ctx:
            self.service.original(self.report_id, "a")
        self.assertEqual(ctx.exception.code, "source_changed")

    def test_get_confirmed_draft_keeps_original_extraction(self):
        self.repo.previous = Review.model_validate({**BODY, "title": "人工核对标题"})
        result = self.service.get_draft(self.report_id, "a")
        self.assertEqual((result["status"], result["title"], result["extracted_text"]),
                         ("confirmed", "人工核对标题", TEXT))


class BoundaryChecks(unittest.TestCase):
    def test_missing_member_rejects_before_report_read(self):
        repo = Mock()
        repo.member.return_value = None
        with self.assertRaises(BusinessError):
            RecordService(repo).reports("missing")
        repo.reports.assert_not_called()

    def test_metric_source_is_derived_from_actual_row(self):
        repo = Mock()
        repo.member.return_value = {"id": "a"}
        repo.metrics.return_value = [{"id": 7, "value": "5.7"}]
        self.assertEqual(RecordService(repo).metrics("a")["items"][0]["source_id"], "metric:7")

    def test_query_arguments_keep_scope_and_modes(self):
        run = Mock(return_value={"status": "retrieved"})
        service = QueryService(run, AsyncMock())
        req = QueryRequest(question="原文", member_id="a", report_ids=["r"], top_k=2, with_answer=False,
                           chunk_version="section200o40v1", retrieval_mode="keyword")
        service.query(req)
        run.assert_called_once_with("原文", "a", ["r"], 2, with_answer=False,
                                    chunk_version="section200o40v1", retrieval_mode="keyword")

    def test_http_dependency_override_reaches_both_query_routes(self):
        service = QueryService(Mock(return_value={"status": "retrieved"}),
                               AsyncMock(return_value={"status": "clarify"}))
        app = FastAPI()
        app.include_router(query_routes.router)
        app.include_router(agent_routes.router)
        app.dependency_overrides[get_query_service] = lambda: service
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/query", json={"question": "原文", "member_id": "a"}).json()["status"], "retrieved")
            self.assertEqual(client.post("/api/agent/query", json={"question": "谁的报告？"}).json()["status"], "clarify")

    def test_validation_rejects_before_service(self):
        runner = Mock()
        app = FastAPI()
        app.include_router(query_routes.router)
        app.dependency_overrides[get_query_service] = lambda: QueryService(runner, AsyncMock())
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/query", json={"question": "  ", "member_id": "a"}).status_code, 422)
        runner.assert_not_called()

    def test_routes_have_no_sql_and_services_have_no_http_import(self):
        base = Path(__file__).parent
        for name in ["record_routes.py", "query_routes.py", "agent_routes.py", "upload_routes.py"]:
            tree = ast.parse((base / name).read_text(encoding="utf-8"))
            self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr == "execute" for n in ast.walk(tree)), name)
        for name in ["records.py", "reports.py", "queries.py"]:
            tree = ast.parse((base / "services" / name).read_text(encoding="utf-8"))
            self.assertFalse(any(isinstance(n, ast.ImportFrom) and (n.module or "").startswith("fastapi") for n in ast.walk(tree)), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

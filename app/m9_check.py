"""M9 离线契约/异常/日志检查。替身通过不作为真实 PostgreSQL 验收。"""
import asyncio
import io
import json
import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app import query_routes, agent_routes, record_routes, upload_routes, rag, agent
from app.api_errors import install_api_contract
from app.dependencies import get_query_service, get_record_service, get_report_service
from app.domain_errors import BusinessError
from app.observability import logger, stage
from app.repositories.records import RecordRepository
from app.schemas.responses import QueryResponse, ERROR_RESPONSES
from app.services.queries import QueryService
from app.services.records import RecordService

MEMBER = {"id": "a", "name": "合成成员", "aliases": []}
EVIDENCE = {"source_id": "chunk:1", "report_id": "r1", "member_id": "a", "content": "合成文本"}


def query_result(status="retrieved", **extra):
    return {"query_id": "q1", "status": status, "answer": None, "citations": [], "evidence": [],
            "latency_ms": 0, "token_usage": {}, "model_calls": 0, **extra}


def agent_result(status="clarify", **extra):
    return {"query_id": "q1", "status": status, "answer": "请选择成员", "citations": [], "evidence": [],
            "tool_calls": 0, "llm_calls": 0, "embedding_calls": 0, "latency_ms": 0, "token_usage": {},
            "evidence_check": {}, "supplemental_calls": 0, "revision_calls": 0,
            "trace_url": "/api/query/q1/trace", **extra}


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(); self.rows = []
    def emit(self, record):
        self.rows.append(json.loads(record.getMessage()))


class ContractChecks(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'M9_LOG_PATH': ''})
        environment.start();self.addCleanup(environment.stop)
        self.capture = Capture()
        self.previous_level = logger.level
        logger.setLevel(logging.INFO); logger.addHandler(self.capture)
        self.addCleanup(logger.setLevel, self.previous_level)
        self.addCleanup(logger.removeHandler, self.capture)
        self.query = Mock(return_value=query_result())
        self.agent = AsyncMock(return_value=agent_result())
        self.records, self.reports = Mock(), Mock()
        self.records.members.return_value = {"items": [MEMBER], "count": 1}
        self.records.reports.return_value = {"member": MEMBER, "items": [], "count": 0, "result_code": "no_records"}
        self.records.metrics.return_value = {"member": MEMBER, "items": [], "count": 0, "result_code": "no_records"}
        self.app = FastAPI(responses=ERROR_RESPONSES)
        for router in [record_routes.router, query_routes.router, agent_routes.router, upload_routes.router]:
            self.app.include_router(router)
        self.app.dependency_overrides[get_query_service] = lambda: QueryService(self.query, self.agent)
        self.app.dependency_overrides[get_record_service] = lambda: self.records
        self.app.dependency_overrides[get_report_service] = lambda: self.reports
        install_api_contract(self.app, configure=False)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def ask(self, **changes):
        return self.client.post('/api/query', json={"question": "合成问题", "member_id": "a", **changes})

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {"code", "message", "request_id", "details"})
        self.assertEqual(body["code"], code)
        self.assertRegex(body["request_id"], r'^[a-f0-9]{32}$')
        self.assertEqual(response.headers['x-request-id'], body['request_id'])
        self.assertNotIn('PRIVATE_SENTINEL', response.text)
        return body

    def test_normal_query_matches_response(self):
        r = self.ask(); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(QueryResponse.model_validate(r.json()).result_code, 'retrieval_only')

    def test_answered_query_requires_and_keeps_evidence(self):
        self.query.return_value=query_result('answered', answer='原文[[chunk:1]]', citations=[EVIDENCE], evidence=[EVIDENCE])
        self.assertEqual(self.ask().json()['result_code'], 'ok')

    def test_empty_report_list_is_business_result(self):
        r=self.client.get('/api/reports?member_id=a')
        self.assertEqual((r.status_code,r.json()['result_code']), (200,'no_records'))

    def test_empty_metric_list_is_business_result(self):
        r=self.client.get('/api/metrics?member_id=a')
        self.assertEqual((r.status_code,r.json()['result_code']), (200,'no_records'))

    def test_insufficient_is_not_system_error(self):
        self.query.return_value=query_result('insufficient', answer='资料不足')
        self.assertEqual((self.ask().status_code,self.ask().json()['result_code']),(200,'insufficient_evidence'))

    def test_no_records_has_distinct_code(self):
        self.query.return_value=query_result('insufficient', answer='没有报告',result_code='no_records')
        self.assertEqual(self.ask().json()['result_code'],'no_records')

    def test_agent_clarification_is_business_result(self):
        r=self.client.post('/api/agent/query',json={'question':'谁的报告？'})
        self.assertEqual((r.status_code,r.json()['result_code']),(200,'clarification_required'))

    def test_request_validation_precedes_query_runner(self):
        self.error(self.ask(question='  '),422,'validation_error');self.query.assert_not_called()

    def test_query_bounds_types_and_enums(self):
        for body in [{'top_k':0},{'top_k':9},{'top_k':True},{'top_k':'2'},{'question':'x'*1001},
                     {'retrieval_mode':'unknown'},{'chunk_version':'missing'},{'unknown':1}]:
            with self.subTest(body=body):self.error(self.ask(**body),422,'validation_error')
        self.query.assert_not_called()

    def test_missing_body_field(self):
        self.error(self.client.post('/api/query',json={'question':'x'}),422,'validation_error')

    def test_malformed_json(self):
        r=self.client.post('/api/query',content='{bad PRIVATE_SENTINEL',headers={'Content-Type':'application/json'})
        self.error(r,422,'validation_error')

    def test_validation_details_omit_submitted_values(self):
        body=self.error(self.ask(top_k='PRIVATE_SENTINEL'),422,'validation_error')
        self.assertTrue(body['details']);self.assertEqual(set(body['details'][0]),{'field','type'})

    def test_report_ids_need_member_scope(self):
        r=self.client.post('/api/agent/query',json={'question':'x','report_ids':['r1']})
        self.error(r,422,'validation_error');self.agent.assert_not_called()

    def test_report_list_item_whitespace_is_trimmed(self):
        self.ask(report_ids=[' r1 ','r1'])
        self.assertEqual(self.query.call_args.args[2],['r1'])

    def test_agent_member_limit(self):
        r=self.client.post('/api/agent/query',json={'question':'x','member_ids':['a']*6})
        self.error(r,422,'validation_error')

    def test_valid_date_closed_interval_reaches_service(self):
        r=self.client.get('/api/metrics?member_id=a&date_from=2026-10-01&date_to=2026-10-31')
        self.assertEqual(r.status_code,200,r.text)
        self.records.metrics.assert_called_once_with('a',None,date(2026,10,1),date(2026,10,31))

    def test_reverse_date_range_is_422(self):
        self.error(self.client.get('/api/metrics?member_id=a&date_from=2026-10-31&date_to=2026-10-01'),422,'validation_error')
        self.records.metrics.assert_not_called()

    def test_bad_date_is_422(self):
        self.error(self.client.get('/api/metrics?member_id=a&date_from=2026-02-30'),422,'validation_error')

    def test_blank_metric_member_is_422(self):
        self.error(self.client.get('/api/metrics',params={'member_id':'   '}),422,'validation_error')

    def test_member_missing_404(self):
        self.records.reports.side_effect=BusinessError('member_not_found','合成成员不存在')
        self.error(self.client.get('/api/reports?member_id=missing'),404,'member_not_found')

    def test_wrong_report_scope_404(self):
        self.records.report.side_effect=BusinessError('report_not_found','不在指定范围')
        self.error(self.client.get('/api/reports/r1?member_id=b'),404,'report_not_found')

    def test_draft_not_found_404(self):
        self.reports.get_draft.side_effect=BusinessError('draft_not_found','草稿不存在')
        self.error(self.client.get('/api/ingest/upload_missing?member_id=a'),404,'draft_not_found')

    def test_source_state_conflict_409(self):
        self.reports.get_draft.side_effect=BusinessError('source_changed','原件改变')
        self.error(self.client.get('/api/ingest/upload_missing?member_id=a'),409,'source_changed')

    def test_missing_route_404(self):
        self.error(self.client.get('/PRIVATE_SENTINEL'),404,'resource_not_found')
        self.assertEqual(self.capture.rows[-1]['route'],'<unmatched>')

    def test_wrong_method_405_keeps_allow(self):
        r=self.client.get('/api/query');self.error(r,405,'method_not_allowed')
        self.assertIn('POST',r.headers['allow'])

    def test_http_detail_is_not_arbitrarily_echoed(self):
        self.query.side_effect=HTTPException(502,{'code':'provider_secret','message':'PRIVATE_SENTINEL'})
        self.error(self.ask(),502,'upstream_error')

    def test_model_timeout_504(self):
        self.query.side_effect=APITimeoutError(request=httpx.Request('POST','https://fake.invalid'))
        self.error(self.ask(),504,'upstream_timeout')

    def test_provider_http_failure_502(self):
        response=httpx.Response(429,request=httpx.Request('POST','https://fake.invalid'))
        self.query.side_effect=APIStatusError('PRIVATE_SENTINEL',response=response,body={'secret':'PRIVATE_SENTINEL'})
        self.error(self.ask(),502,'upstream_error')

    def test_provider_connection_failure_502(self):
        self.query.side_effect=APIConnectionError(request=httpx.Request('POST','https://fake.invalid'))
        self.error(self.ask(),502,'upstream_error')

    def test_database_failure_503(self):
        self.query.side_effect=psycopg.OperationalError('PRIVATE_SENTINEL')
        self.error(self.ask(),503,'database_error')

    def test_storage_failure_503(self):
        self.query.side_effect=OSError('PRIVATE_SENTINEL')
        self.error(self.ask(),503,'storage_error')

    def test_unexpected_exception_500(self):
        self.query.side_effect=RuntimeError('PRIVATE_SENTINEL')
        self.error(self.ask(),500,'internal_error')

    def test_output_missing_required_fields_is_500(self):
        self.query.return_value={'status':'retrieved'}
        self.error(self.ask(),500,'response_contract_violation')

    def test_output_incorrect_count_is_500(self):
        self.records.members.return_value={'items':[MEMBER],'count':9}
        self.error(self.client.get('/api/members'),500,'response_contract_violation')

    def test_cross_member_output_is_contract_violation(self):
        other={'id':'r1','member_id':'b','title':'合成','report_date':'2026-10-01',
               'report_type':'体检','source_type':'synthetic','status':'confirmed'}
        self.records.reports.return_value={'member':MEMBER,'items':[other],'count':1,'result_code':'ok'}
        self.error(self.client.get('/api/reports?member_id=a'),500,'response_contract_violation')

    def test_answer_without_citation_is_500(self):
        self.query.return_value=query_result('answered',answer='PRIVATE_SENTINEL')
        self.error(self.ask(),500,'response_contract_violation')

    def test_response_uses_utf8_and_request_header(self):
        r=self.ask();self.assertIn('application/json',r.headers['content-type']);self.assertIn('x-request-id',r.headers)

    def test_error_request_id_finds_one_completion_log(self):
        body=self.error(self.ask(top_k=0),422,'validation_error')
        records=[r for r in self.capture.rows if r['event']=='request.completed' and r['request_id']==body['request_id']]
        self.assertEqual(len(records),1);self.assertEqual(records[0]['http_status'],422)
        self.assertEqual(records[0]['error_code'],'validation_error')

    def test_stage_and_completion_share_request_id(self):
        r=self.ask();rid=r.headers['x-request-id']
        records=[x for x in self.capture.rows if x['request_id']==rid]
        self.assertTrue(any(x.get('stage')=='query.pipeline' for x in records))
        self.assertEqual(records[-1]['route'],'/api/query');self.assertGreaterEqual(records[-1]['duration_ms'],0)

    def test_request_logs_omit_body_query_and_exception_text(self):
        self.query.side_effect=RuntimeError('PRIVATE_SENTINEL')
        self.client.post('/api/query?secret=PRIVATE_SENTINEL',json={'question':'PRIVATE_SENTINEL','member_id':'a'})
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.capture.rows))

    def test_client_cannot_force_request_id(self):
        r=self.client.post('/api/query',json={'question':'x','member_id':'a'},headers={'X-Request-ID':'PRIVATE_SENTINEL'})
        self.assertNotEqual(r.headers['x-request-id'],'PRIVATE_SENTINEL')

    def test_concurrent_request_contexts_do_not_mix(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses=list(pool.map(lambda _:self.ask(),range(6)))
        ids=[r.headers['x-request-id'] for r in responses];self.assertEqual(len(set(ids)),6)
        for rid in ids:
            rows=[r for r in self.capture.rows if r['request_id']==rid]
            self.assertEqual(sum(r['event']=='request.completed' for r in rows),1)
            self.assertEqual(sum(r.get('stage')=='query.pipeline' for r in rows),1)

    def test_all_json_routes_have_success_and_error_schema(self):
        spec=self.app.openapi()
        for path,methods in spec['paths'].items():
            for method,operation in methods.items():
                if method not in {'get','post'}:continue
                responses=operation['responses']
                if not path.endswith('/file'):
                    self.assertIn('schema',responses['200']['content']['application/json'],path)
                for status in ['422','500','502','503','504']:
                    self.assertEqual(responses[status]['content']['application/json']['schema']['$ref'],
                                     '#/components/schemas/ErrorResponse',path)

    def test_install_is_idempotent(self):
        n=len(self.app.user_middleware);install_api_contract(self.app,configure=False)
        self.assertEqual(len(self.app.user_middleware),n)

    def test_production_main_middleware_and_error_handler(self):
        from app.main import app
        overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_query_service] = lambda: QueryService(self.query, self.agent)
        try:
            with patch('app.main.init_db'):
                with TestClient(app, raise_server_exceptions=False) as client:
                    self.query.side_effect=RuntimeError('PRIVATE_SENTINEL')
                    self.error(client.post('/api/query',json={'question':'x','member_id':'a'}),500,'internal_error')
                    self.query.side_effect=None
                    self.assertEqual(client.post('/api/query',json={'question':'x','member_id':'a'}).status_code,200)
        finally:
            app.dependency_overrides.clear();app.dependency_overrides.update(overrides)

    def test_production_health_matches_schema(self):
        from app.main import app
        with patch('app.main.init_db'),patch('app.main.database_status',return_value={'status':'ok','name':'synthetic','pgvector':'controlled'}):
            with TestClient(app,raise_server_exceptions=False) as client:
                response=client.get('/health')
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['stage'],'M12')


class DataBoundaryChecks(unittest.TestCase):
    def test_date_filter_sql_is_parameterized_and_member_scoped(self):
        conn=Mock();conn.execute.return_value.fetchall.return_value=[]
        RecordRepository(conn).metrics('a','fasting_glucose',date(2026,10,1),date(2026,10,31))
        sql,args=conn.execute.call_args.args
        self.assertIn('r.member_id = %s',sql);self.assertIn('r.status',sql)
        self.assertIn('r.report_date >= %s',sql);self.assertIn('r.report_date <= %s',sql)
        self.assertEqual(args,['a','fasting_glucose',date(2026,10,1),date(2026,10,31)])

    def test_missing_member_prevents_metric_read(self):
        repo=Mock();repo.member.return_value=None
        with self.assertRaises(BusinessError):RecordService(repo).metrics('missing')
        repo.metrics.assert_not_called()

    def test_raw_rag_no_reports_and_no_hits_are_distinct(self):
        def retrieve_empty(*args,**kwargs):args[4].append({'step':'scope','report_ids':[]});return []
        def retrieve_no_hits(*args,**kwargs):args[4].append({'step':'scope','report_ids':['r1']});return []
        for fake,expected in [(retrieve_empty,'no_records'),(retrieve_no_hits,'insufficient_evidence')]:
            with patch.object(rag,'retrieve',side_effect=fake),patch.object(rag,'save_log'):
                self.assertEqual(rag.run_query('x','a')['result_code'],expected)

    def test_raw_agent_no_reports_skips_model(self):
        with patch.object(agent,'prepare_scope',return_value={'reports':[]}),patch.object(agent,'save_agent_log'),\
             patch.object(agent,'make_client') as model:
            result=asyncio.run(agent.run_agent('x',['a']))
        self.assertEqual(result['result_code'],'no_records');model.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
